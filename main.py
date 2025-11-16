# main.py
# PartyRadar — монолитная версия под Aiogram 3.x (Webhook-friendly)
# Требует: aiogram==3.x, aiohttp, python-dotenv, geopy

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

# ===================== CONFIG =====================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

LOGO_URL = ""  # можно указать RAW-URL логотипа, если локального файла нет

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Файлы хранения
EVENTS_FILE = "events.json"
BANNERS_FILE = "banners.json"
USERS_FILE = "users.json"
PAYMENTS_FILE = "payments.json"

# Радиус поиска / пуша
DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2

# Баннеры
MAX_ACTIVE_BANNERS = 3

# ===================== ТАРИФЫ И ПЛАТНЫЕ ОПЦИИ =====================

# Все платные функции — в одном месте
PAID_OPTIONS = {
    "duration": {
        # key: часы, value: (label, price)
        24: {"label": "🕐 1 день (бесплатно)", "price": 0.0},
        72: {"label": "⏱ 3 дня — $0.90", "price": 0.90},
        168: {"label": "⏱ 7 дней — $1.90", "price": 1.90},
        720: {"label": "⏱ 30 дней — $4.90", "price": 4.90},
    },
    "top": {"label": "⭐ ТОП — $1.50", "price": 1.50},
    "banner": {"label": "🖼 Баннер — $7.00", "price": 7.00},
    "push": {"label": "📣 PUSH 30 км — $1.50", "price": 1.50},
}

# Старые тарифы продлений (сохраняем для совместимости авто-пуша продления)
EXTEND_TARIFFS_USD = {
    24: 1.0,
    72: 3.0,
    168: 5.0,
    720: 6.0,
}

# Сроки баннеров (используются для покупки и продления)
BANNER_DURATIONS = {
    "📅 1 день — $7": (1, 7.0),
    "📅 3 дня — $15": (3, 15.0),
    "📅 7 дней — $30": (7, 30.0),
    "📅 15 дней — $45": (15, 45.0),
    "📅 30 дней — $75": (30, 75.0),
}

PUSH_PRICE_USD = PAID_OPTIONS["push"]["price"]

# ===================== JSON HELPERS =====================

def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_events() -> List[dict]:
    return _load_json(EVENTS_FILE, [])


def _save_events(data: List[dict]):
    _save_json(EVENTS_FILE, data)


def _load_banners() -> List[dict]:
    return _load_json(BANNERS_FILE, [])


def _save_banners(data: List[dict]):
    _save_json(BANNERS_FILE, data)


def _load_users() -> Dict[str, dict]:
    return _load_json(USERS_FILE, {})


def _save_users(data: Dict[str, dict]):
    _save_json(USERS_FILE, data)


def _load_payments() -> Dict[str, dict]:
    data = _load_json(PAYMENTS_FILE, {})
    if isinstance(data, list):
        data = {}
    return data


def _save_payments(data: Dict[str, dict]):
    _save_json(PAYMENTS_FILE, data)


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
            logging.warning(f"⚠️ CryptoCloud вернул ошибку: {data}")
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


# ===================== FSM =====================

class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt_or_price = State()
    media = State()
    location = State()
    contact = State()
    constructor = State()  # экран выбора платных функций
    payment = State()      # ожидание оплаты пакета
    preview = State()      # тех. состояние для предпросмотра


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
            [KeyboardButton(text="📩 Связаться с нами")]
        ],
        resize_keyboard=True
    )


def kb_back():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    )


def kb_skip_back():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➡️ Пропустить"), KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


def kb_media_step():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⬅ Назад")]
        ],
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
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


def kb_search_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Все события рядом")],
            [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="💼 Работа")],
            [KeyboardButton(text="✨ Покажи себя"), KeyboardButton(text="🔍 Ищу тебя")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


def kb_send_location():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


def kb_banner_duration():
    rows = [
        [KeyboardButton(text="📅 1 день — $7"), KeyboardButton(text="📅 3 дня — $15")],
        [KeyboardButton(text="📅 7 дней — $30"), KeyboardButton(text="📅 15 дней — $45")],
        [KeyboardButton(text="📅 30 дней — $75")],
        [KeyboardButton(text="⬅ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_payment():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Получить ссылку на оплату")],
            [KeyboardButton(text="✅ Я оплатил")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


def kb_constructor(summary_text: str) -> ReplyKeyboardMarkup:
    """
    Конструктор платных опций: одна клавиатура для всего.
    """
    duration_buttons = [
        [KeyboardButton(text=PAID_OPTIONS["duration"][24]["label"])],
        [KeyboardButton(text=PAID_OPTIONS["duration"][72]["label"])],
        [KeyboardButton(text=PAID_OPTIONS["duration"][168]["label"])],
        [KeyboardButton(text=PAID_OPTIONS["duration"][720]["label"])],
    ]
    extra_buttons = [
        [KeyboardButton(text="⭐ ТОП (переключить)")],
        [KeyboardButton(text="🖼 Баннер (переключить)")],
        [KeyboardButton(text="📣 PUSH 30 км (переключить)")],
        [KeyboardButton(text="👁 Предпросмотр объявления")],
        [KeyboardButton(text="💥 Оплатить и опубликовать")],
        [KeyboardButton(text="⬅ Назад")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=duration_buttons + extra_buttons,
        resize_keyboard=True,
        input_field_placeholder=summary_text[:64] if summary_text else None
    )


def kb_banner_location():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📍 Отправить мою геолокацию", callback_data="bn_geo_my")],
            [InlineKeyboardButton(text="📍 Выбрать точку на карте", callback_data="bn_geo_point")],
            [InlineKeyboardButton(text="➡️ Пропустить", callback_data="bn_geo_skip")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="bn_geo_back")],
        ]
    )


# ===================== TEXT HELPERS =====================

def sanitize(text: str) -> str:
    return re.sub(r"[^\S\r\n]+", " ", text or "").strip()


def format_event_card(ev: dict, with_distance: Optional[float] = None) -> str:
    desc = f"\n📝 {sanitize(ev.get('description') or '')}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {sanitize(ev.get('contact') or '')}" if ev.get("contact") else ""
    price_part = f"\n💵 Цена: {sanitize(ev.get('price') or '')}" if ev.get("price") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    dist = f"\n📏 Расстояние: {with_distance:.1f} км" if with_distance is not None else ""
    dt_str = ""
    if ev.get("datetime"):
        try:
            dt = datetime.fromisoformat(ev["datetime"])
            dt_str = f"\n📅 {dt.strftime('%d.%m.%Y %H:%M')}"
        except Exception:
            pass
    return (
        f"📌 <b>{sanitize(ev['title'])}</b>{top}\n"
        f"📍 {sanitize(ev['category'])}{desc}"
        f"{dt_str}{price_part}{contact}{dist}"
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


async def send_event_media(chat_id: int, ev: dict, with_distance: Optional[float] = None, preview: bool = False):
    text = format_event_card(ev, with_distance)
    buttons = []

    if ev.get("lat") is not None and ev.get("lon") is not None:
        gmap = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        buttons.append([InlineKeyboardButton(text="🌐 Открыть в Google Maps", url=gmap)])

    if not preview and ev.get("id") is not None:
        row = [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add:{ev['id']}")]
        if ev.get("author") and int(ev["author"]) == int(chat_id):
            row.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ev_del:{ev['id']}"))
        buttons.append(row)

    ikb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    media = ev.get("media_files") or []
    for f in media:
        if f.get("is_local"):
            f["file_id"] = FSInputFile(f["file_id"])

    if len(media) > 1:
        group = []
        for i, f in enumerate(media):
            caption = text if i == 0 else None
            if f["type"] == "photo":
                group.append(InputMediaPhoto(media=f["file_id"], caption=caption, parse_mode="HTML"))
            elif f["type"] == "video":
                group.append(InputMediaVideo(media=f["file_id"], caption=caption, parse_mode="HTML"))
        await bot.send_media_group(chat_id, group)
        if ikb:
            await bot.send_message(chat_id, " ", reply_markup=ikb)
    elif len(media) == 1:
        f = media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=ikb)
        elif f["type"] == "video":
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=ikb)
    else:
        await bot.send_message(chat_id, text, reply_markup=ikb)


async def send_banner(chat_id: int, b: dict):
    cap = format_banner_caption(b)
    if b.get("media_type") == "photo" and b.get("file_id"):
        await bot.send_photo(chat_id, b["file_id"], caption=cap, parse_mode="HTML")
    elif b.get("media_type") == "video" and b.get("file_id"):
        await bot.send_video(chat_id, b["file_id"], caption=cap, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, cap, parse_mode="HTML")


async def send_push_for_event(ev: dict) -> int:
    """Отправка события всем пользователям в радиусе DEFAULT_RADIUS_KM."""
    lat = ev.get("lat")
    lon = ev.get("lon")
    if lat is None or lon is None:
        return 0

    users = _load_users()
    sent = 0
    event_loc = (lat, lon)

    for uid, info in users.items():
        loc = (info.get("last_location") or {})
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
            logging.exception(f"Ошибка отправки PUSH пользователю {uid}: {e}")

    return sent


# ===================== МОДЕРАЦИЯ =====================

FORBIDDEN_KEYWORDS_GROUPS = {
    "adult": [
        "интим", "эскорт", "секс услуги", "sex услуги", "минет", "onlyfans", "онлифанс", "порн"
    ],
    "drugs": [
        "закладка", "закладки", "наркотик", "наркота", "метамфетамин", "амфетамин",
        "героин", "кокаин", "марихуана", "шишки", "спайс"
    ],
    "weapons": [
        "оружие", "пистолет", "автомат калашникова", "ak-47", "ak47", "нож-бабочка",
        "куплю гранату", "продам гранату", "продам оружие"
    ],
    "gambling": [
        "ставки на спорт", "казино", "1xbet", "1хбет", "букмекерская контора",
        "игровые автоматы", "слоты", "рулетка"
    ],
    "fraud": [
        "легкий заработок без вложений", "быстрый заработок", "доход 1000$ в день",
        "пирамида", "финансовая пирамида", "обещаю доход", "инвестиции без риска"
    ],
}

FORBIDDEN_DOMAINS = [
    "onlyfans.com", "pornhub.com", "xvideos.com", "xhamster.com",
    "1xbet.com", "ggbet", "mostbet", "casino", "pin-up"
]

SUSPICIOUS_SHORTLINKS = [
    "bit.ly", "tinyurl.com", "cutt.ly", "t.me/joinchat", "t.me/+"
]


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
                    return False, "Объявление похоже на откровенный/18+ контент, такое размещать нельзя."
                if group == "drugs":
                    return False, "Объявление похоже на рекламу или продажу запрещённых веществ."
                if group == "weapons":
                    return False, "Объявление похоже на рекламу или продажу оружия."
                if group == "gambling":
                    return False, "Объявление похоже на рекламу азартных игр."
                if group == "fraud":
                    return False, "Объявление похоже на подозрительную финансовую схему."
                return False, "Объявление не прошло автоматическую модерацию."

    return True, None


def check_event_moderation(data: dict) -> Tuple[bool, Optional[str]]:
    parts = []
    for k in ("title", "description", "contact", "category"):
        if data.get(k):
            parts.append(str(data[k]))
    return _check_text_moderation("\n".join(parts))


# ===================== ЛИМИТ БЕСПЛАТНЫХ ОБЪЯВЛЕНИЙ =====================

def can_use_free_in_category(user_id: int, category: str) -> bool:
    """
    1 бесплатное событие на одну категорию в сутки.
    Считаем по полю is_free=True и created >= now-24h.
    """
    events = _load_events()
    now = datetime.now()
    cutoff = now - timedelta(hours=24)
    for ev in events:
        if int(ev.get("author", 0)) != int(user_id):
            continue
        if ev.get("category") != category:
            continue
        if not ev.get("is_free"):
            continue
        created = _safe_dt(ev.get("created"))
        if created and created >= cutoff:
            return False
    return True


# ===================== START / WELCOME =====================

async def send_logo_then_welcome(m: Message):
    logo_path = None
    for ext in ("png", "jpg", "jpeg"):
        p = f"logo.{ext}"
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

    welcome_text = (
        "👋 Добро пожаловать в <b>PartyRadar</b>!\n"
        "📍 Сервис по поиску людей и событий по геолокации.\n\n"
        "🎉 Категории:\n"
        "• Вечеринки, встречи по интересам, свидания, спорт.\n"
        "• 🛒 Маркет — «Куплю/Продам» рядом.\n"
        "• 💼 Работа — вакансии и те, кто ищет работу.\n"
        "• ✨ Покажи себя — самопрезентация, соцсети, знакомства.\n"
        "• 🔍 Ищу тебя — поиск людей и пропавших питомцев.\n"
        "• 🎊 Поздравления — короткие объявления без даты.\n\n"
        "📅 Базовые объявления: 1 бесплатное событие на категорию в сутки.\n"
        "💎 Дополнительно: платные сроки, ТОП, баннеры, PUSH по радиусу 30 км.\n\n"
        "👇 Выбирай действие из меню:"
    )
    await m.answer(welcome_text, reply_markup=kb_main())

    # Показ баннеров по региону пользователя
    banners = _load_banners()
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    last_loc = (u.get("last_location") or {})
    u_lat = last_loc.get("lat")
    u_lon = last_loc.get("lon")
    now = datetime.now()

    actives = []
    if u_lat is not None and u_lon is not None:
        for b in banners:
            exp = _safe_dt(b.get("expire"))
            if not exp or exp <= now:
                continue
            b_lat = b.get("lat")
            b_lon = b.get("lon")
            if b_lat is None or b_lon is None:
                continue
            try:
                dist = geodesic((u_lat, u_lon), (b_lat, b_lon)).km
            except Exception:
                continue
            if dist <= DEFAULT_RADIUS_KM:
                actives.append(b)
        actives.sort(key=lambda x: x.get("id", 0), reverse=True)
        for b in actives[:MAX_ACTIVE_BANNERS]:
            try:
                await send_banner(m.chat.id, b)
            except Exception:
                pass


@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await send_logo_then_welcome(m)


# ===================== SUPPORT =====================

@dp.message(F.text == "📩 Связаться с нами")
async def support(m: Message):
    await m.answer(
        "💬 Идеи, жалобы, предложения — пиши администратору:\n"
        "👉 <b>@drscorohod</b>\n\n"
        "Мы читаем все сообщения и стараемся улучшать сервис ❤️",
        reply_markup=kb_main()
    )


# ===================== СОЗДАНИЕ СОБЫТИЯ =====================

@dp.message(F.text == "➕ Создать событие")
async def create_event_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer("📝 Введи <b>название</b> события:", reply_markup=kb_back())


@dp.message(AddEvent.title)
async def ev_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await state.update_data(title=sanitize(m.text))
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи <b>описание</b> события:", reply_markup=kb_back())


@dp.message(AddEvent.description)
async def ev_desc(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.title)
        return await m.answer("📝 Введи название события:", reply_markup=kb_back())
    await state.update_data(description=sanitize(m.text))
    await state.set_state(AddEvent.category)
    await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())


@dp.message(AddEvent.category)
async def ev_cat(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer("🧾 Введи описание события:", reply_markup=kb_back())

    cat = sanitize(m.text)
    await state.update_data(category=cat)

    # Описания рубрик
    if cat == "✨ Покажи себя":
        text = (
            "✨ <b>Покажи себя</b> — self promo, самопрезентация, реклама себя и соцсетей.\n"
            "Расскажи о себе, прикрепи фото/видео, дай ссылки на соцсети.\n"
        )
    elif cat == "🔍 Ищу тебя":
        text = (
            "🔍 <b>Ищу тебя</b> — поиск людей и потерянных питомцев.\n"
            "Опиши, кого и где ты ищешь, прикрепи фото, если есть.\n"
        )
    elif cat == "🎊 Поздравления":
        text = (
            "🎊 <b>Поздравления</b> — короткие объявления без даты.\n"
            "Поздравь друга, родных или всех вокруг, поделись хорошими новостями.\n"
        )
    elif cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
        text = (
            "💼 <b>Работа</b> — рубрика «Ищу» и «Предлагаю».\n"
            "Без привязки к дате, просто опиши условия / свои навыки.\n"
        )
    else:
        text = ""

    if text:
        await m.answer(text)

    # Маркет: цена вместо даты
    if cat in ("🛒 Куплю", "💰 Продам"):
        await state.set_state(AddEvent.dt_or_price)
        return await m.answer(
            "💵 Укажи цену (можно с символом валюты):\n"
            "Например: <b>150€</b>, <b>200$</b>, <b>5000₽</b> или <b>договорная</b>.",
            reply_markup=kb_back()
        )

    # Работа / покажи себя / ищу тебя / поздравления — без даты
    if cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу", "✨ Покажи себя", "🔍 Ищу тебя", "🎊 Поздравления"):
        await state.update_data(datetime=None, price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n"
            "📍 Скрепка → Геопозиция → точка на карте.\n"
            "⚠ Аудио и кружки не поддерживаются.",
            reply_markup=kb_media_step()
        )

    # Обычные события — спрашиваем дату/время
    await state.set_state(AddEvent.dt_or_price)
    await m.answer(
        "📆 Введи дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n"
        "Пример: 25.10.2025 19:30",
        reply_markup=kb_back()
    )


@dp.message(AddEvent.dt_or_price)
async def ev_dt_or_price(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category)
        return await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())

    data = await state.get_data()
    cat = data.get("category")

    if cat in ("🛒 Куплю", "💰 Продам"):
        await state.update_data(price=sanitize(m.text), datetime=None, media_files=[])
    else:
        try:
            dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
            if dt <= datetime.now():
                return await m.answer("⚠ Нельзя указывать прошедшее время.", reply_markup=kb_back())
        except ValueError:
            return await m.answer("⚠ Неверный формат. Пример: 25.10.2025 19:30", reply_markup=kb_back())
        await state.update_data(datetime=dt.isoformat(), price=None, media_files=[])

    await state.set_state(AddEvent.media)
    await m.answer(
        "📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n"
        "📍 Скрепка → Геопозиция → точка на карте.\n"
        "⚠ Аудио и кружки не поддерживаются.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def ev_media(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    if len(files) >= 3:
        return await m.answer("⚠ Уже 3 файла. Отправь геолокацию.", reply_markup=kb_media_step())

    if m.photo:
        files.append({"type": "photo", "file_id": m.photo[-1].file_id})
    elif m.video:
        files.append({"type": "video", "file_id": m.video.file_id})
    await state.update_data(media_files=files)
    left = 3 - len(files)
    await m.answer(
        f"✅ Файл добавлен ({len(files)}/3). "
        + ("Можно добавить ещё или " if left else "")
        + "отправь геолокацию для следующего шага.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE}))
async def ev_media_unsupported(m: Message, state: FSMContext):
    await m.answer("⚠ Аудио и кружки не поддерживаются. Прикрепи фото/видео.", reply_markup=kb_media_step())


@dp.message(AddEvent.media, F.text == "⬅ Назад")
async def ev_media_back(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    category = data.get("category")

    if files:
        files.pop()
        await state.update_data(media_files=files)
        return await m.answer(f"🗑 Удалён последний файл ({len(files)}/3).", reply_markup=kb_media_step())

    if category in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу", "✨ Покажи себя", "🔍 Ищу тебя", "🎊 Поздравления"):
        await state.set_state(AddEvent.category)
        return await m.answer("🧭 Вернулись к выбору категории:", reply_markup=kb_categories())

    await state.set_state(AddEvent.dt_or_price)
    await m.answer("📆 Вернулись к дате/времени. Введи ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=kb_back())


@dp.message(AddEvent.media, F.location)
async def ev_media_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)

    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    u["last_location"] = {"lat": m.location.latitude, "lon": m.location.longitude}
    u["last_seen"] = datetime.now().isoformat()
    users[str(m.from_user.id)] = u
    _save_users(users)

    await state.set_state(AddEvent.location)
    await m.answer("Геолокация получена ✅\n\nТеперь укажи контакт для связи (или напиши «Пропустить»).", reply_markup=kb_back())


@dp.message(AddEvent.location)
async def ev_location_step(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer("Вернулись к медиафайлам:", reply_markup=kb_media_step())

    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=sanitize(m.text))
    else:
        await state.update_data(contact=None)

    # Переходим к конструктору платных функций
    await state.set_state(AddEvent.constructor)
    data = await state.get_data()
    summary = await build_constructor_summary(m.from_user.id, data)
    await m.answer(
        "⚙️ <b>Конструктор платных функций</b>\n"
        "Здесь ты настраиваешь срок показа, ТОП, баннер и PUSH.\n"
        "Одна оплата = сумма всех выбранных опций.\n\n"
        + summary,
        reply_markup=kb_constructor(summary)
    )


async def build_constructor_summary(user_id: int, data: dict) -> str:
    category = data.get("category")
    selected_duration = data.get("duration_hours", 24)
    selected_top = data.get("opt_top", False)
    selected_banner = data.get("opt_banner", False)
    selected_push = data.get("opt_push", False)

    # Проверка лимита бесплатного события по категории
    free_allowed = can_use_free_in_category(user_id, category) if category else False
    duration_cfg = PAID_OPTIONS["duration"][selected_duration]
    dur_price = duration_cfg["price"]

    # Если выбран 1 день бесплатно, но лимит исчерпан — заставим выбрать платный срок позже
    if selected_duration == 24 and not free_allowed:
        dur_line = "🕐 Срок: 1 день — <b>лимит бесплатных объявлений в этой категории на сегодня исчерпан</b>."
        dur_price = 0.0
    else:
        dur_line = f"🕐 Срок: {duration_cfg['label']}"

    top_line = "⭐ ТОП: включен" if selected_top else "⭐ ТОП: выключен"
    banner_line = "🖼 Баннер: включен" if selected_banner else "🖼 Баннер: выключен"
    push_line = "📣 PUSH 30 км: включен" if selected_push else "📣 PUSH 30 км: выключен"

    total = 0.0
    # Считаем только если бесплатный срок ещё разрешён или выбран платный
    if selected_duration != 24:
        total += dur_price
    else:
        if free_allowed:
            dur_price = 0.0
        else:
            dur_price = 0.0  # фактически заставим выбрать платный вариант другой кнопкой

    if selected_top:
        total += PAID_OPTIONS["top"]["price"]
    if selected_banner:
        total += PAID_OPTIONS["banner"]["price"]
    if selected_push:
        total += PAID_OPTIONS["push"]["price"]

    total_line = f"💰 Итог: <b>${total:.2f}</b>. " + ("(бесплатно)" if total == 0 else "")
    return "\n".join([dur_line, top_line, banner_line, push_line, total_line])


@dp.message(AddEvent.constructor)
async def ev_constructor(m: Message, state: FSMContext):
    data = await state.get_data()
    txt = m.text or ""

    if txt == "⬅ Назад":
        await state.set_state(AddEvent.location)
        return await m.answer("Вернулись к вводу контакта. Укажи контакт или напиши «Пропустить».", reply_markup=kb_back())

    # Выбор длительности
    for hours, cfg in PAID_OPTIONS["duration"].items():
        if txt == cfg["label"]:
            # Если это бесплатный вариант и лимит исчерпан — отклоняем
            if hours == 24 and not can_use_free_in_category(m.from_user.id, data.get("category")):
                await m.answer(
                    "⚠ Лимит бесплатных объявлений в этой категории на сегодня исчерпан.\n\n"
                    "Выбери платный срок размещения (3, 7 или 30 дней).",
                )
            else:
                await state.update_data(duration_hours=hours)
            summary = await build_constructor_summary(m.from_user.id, await state.get_data())
            return await m.answer("Обновлённые настройки:\n\n" + summary, reply_markup=kb_constructor(summary))

    # Переключатели
    if txt == "⭐ ТОП (переключить)":
        await state.update_data(opt_top=not data.get("opt_top", False))
    elif txt == "🖼 Баннер (переключить)":
        await state.update_data(opt_banner=not data.get("opt_banner", False))
    elif txt == "📣 PUSH 30 км (переключить)":
        await state.update_data(opt_push=not data.get("opt_push", False))
    elif txt == "👁 Предпросмотр объявления":
        return await show_preview(m, state)
    elif txt == "💥 Оплатить и опубликовать":
        return await constructor_pay_or_publish(m, state)
    else:
        return await m.answer("Выбери опцию из меню конструктора.", reply_markup=kb_constructor(
            await build_constructor_summary(m.from_user.id, data))
        )

    # После переключателя — пересобираем summary
    summary = await build_constructor_summary(m.from_user.id, await state.get_data())
    await m.answer("Обновлённые настройки:\n\n" + summary, reply_markup=kb_constructor(summary))


async def show_preview(m: Message, state: FSMContext):
    data = await state.get_data()
    media_files = data.get("media_files", [])
    ev = {
        "id": None,
        "author": m.from_user.id,
        "title": data.get("title"),
        "description": data.get("description"),
        "category": data.get("category"),
        "datetime": data.get("datetime"),
        "price": data.get("price"),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "media_files": media_files,
        "contact": data.get("contact"),
        "is_top": data.get("opt_top", False),
    }
    await m.answer("👁 Предпросмотр объявления (как увидят другие пользователи):")
    await send_event_media(m.chat.id, ev, preview=True)
    summary = await build_constructor_summary(m.from_user.id, data)
    await m.answer("Если всё ок — жми «💥 Оплатить и опубликовать».\n\n" + summary,
                   reply_markup=kb_constructor(summary))


async def constructor_pay_or_publish(m: Message, state: FSMContext):
    data = await state.get_data()

    # Модерация
    ok, reason = check_event_moderation(data)
    if not ok:
        await state.clear()
        return await m.answer(reason + "\n\nПопробуй переписать текст более нейтрально 🙏", reply_markup=kb_main())

    user_id = m.from_user.id
    category = data.get("category")
    duration_hours = data.get("duration_hours", 24)
    free_allowed = can_use_free_in_category(user_id, category) if category else False

    # Если пользователь выбрал 1 день, но лимит бесплатных исчерпан — заставляем выбрать платный
    if duration_hours == 24 and not free_allowed:
        return await m.answer(
            "⚠ Лимит бесплатных событий в этой категории на сегодня уже использован.\n"
            "Выбери платный срок размещения (3, 7 или 30 дней).",
            reply_markup=kb_constructor(await build_constructor_summary(user_id, data))
        )

    # Считаем сумму
    total = 0.0
    # срок
    if duration_hours != 24:
        total += PAID_OPTIONS["duration"][duration_hours]["price"]
    # топ
    if data.get("opt_top", False):
        total += PAID_OPTIONS["top"]["price"]
    # баннер
    if data.get("opt_banner", False):
        total += PAID_OPTIONS["banner"]["price"]
    # push
    if data.get("opt_push", False):
        total += PAID_OPTIONS["push"]["price"]

    if total <= 0:
        # Всё бесплатно → публикуем сразу
        await publish_event_with_options(m, data, duration_hours, is_free=True)
        await state.clear()
        return await m.answer(
            "✅ Событие опубликовано бесплатно.\n"
            "📤 Ты можешь переслать объявление в чаты и каналы.",
            reply_markup=kb_main()
        )

    # Платный пакет
    await state.set_state(AddEvent.payment)
    await state.update_data(payment_total=total)

    await m.answer(
        f"💰 К оплате: <b>${total:.2f}</b>.\n\n"
        "Нажми «💳 Получить ссылку на оплату», оплати и затем нажми «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddEvent.payment, F.text == "⬅ Назад")
async def ev_payment_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.constructor)
    summary = await build_constructor_summary(m.from_user.id, await state.get_data())
    await m.answer("Вернулись к настройке опций.\n\n" + summary, reply_markup=kb_constructor(summary))


@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def ev_payment_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    total = data.get("payment_total", 0.0)
    if total <= 0:
        return await m.answer("Сумма к оплате не определена. Вернись и попробуй заново.", reply_markup=kb_constructor(
            await build_constructor_summary(m.from_user.id, data))
        )

    order_id = f"eventpack_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, invoice_uuid = await cc_create_invoice(total, order_id, "PartyRadar: пакет опций для события")
    if not link or not invoice_uuid:
        return await m.answer("⚠ Не удалось создать счёт. Проверь настройки оплаты.", reply_markup=kb_payment())

    payments = _load_payments()
    payments[str(m.from_user.id)] = {
        "type": "event_pack",
        "user_id": m.from_user.id,
        "invoice_uuid": invoice_uuid,
        "payload": data
    }
    _save_payments(payments)

    await state.update_data(_pay_uuid=invoice_uuid)

    await m.answer(
        f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажми «✅ Я оплатил».\n\n"
        "⚠ Платёжная система может взять небольшую комиссию.",
        reply_markup=kb_payment()
    )


@dp.message(AddEvent.payment, F.text == "✅ Я оплатил")
async def ev_payment_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    invoice_uuid = data.get("_pay_uuid")
    duration_hours = data.get("duration_hours", 24)

    if not invoice_uuid:
        return await m.answer("⚠ Не найден идентификатор счёта. Запроси ссылку снова.", reply_markup=kb_payment())

    await m.answer("🔍 Проверяю оплату...")
    paid = await cc_is_paid(invoice_uuid)
    if not paid:
        return await m.answer(
            "❌ Оплата пока не найдена.\n"
            "Если ты только что оплатил — подожди минуту и нажми ещё раз.",
            reply_markup=kb_payment()
        )

    await m.answer("✅ Оплата подтверждена! Публикую событие...")
    await publish_event_with_options(m, data, duration_hours, is_free=(duration_hours == 24))
    await state.clear()

    await m.answer(
        "🎉 Событие опубликовано!\n"
        "📤 Ты можешь переслать объявление в чаты и каналы.",
        reply_markup=kb_main()
    )


async def publish_event_with_options(m: Message, data: dict, hours: int, is_free: bool):
    media_files = data.get("media_files", [])
    if not media_files:
        for ext in ("png", "jpg", "jpeg"):
            p = f"logo.{ext}"
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
        "datetime": data.get("datetime"),
        "price": data.get("price"),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "media_files": media_files,
        "contact": data.get("contact"),
        "expire": expires.isoformat(),
        "created": now.isoformat(),
        "notified": False,
        "is_top": data.get("opt_top", False),
        "top_expire": None,
        "is_free": bool(is_free),
    }

    # Если включён ТОП — ставим срок ТОП равный сроку показа
    if ev["is_top"]:
        ev["top_expire"] = expires.isoformat()
        ev["top_paid_at"] = now.isoformat()

    events.append(ev)
    _save_events(events)

    # Отправляем предпросмотр (уже как опубликованное с кнопками)
    await send_event_media(m.chat.id, ev)

    # Баннер
    if data.get("opt_banner", False):
        await activate_banner_from_event(ev)

    # PUSH
    if data.get("opt_push", False):
        sent = await send_push_for_event(ev)
        try:
            await m.answer(f"📣 PUSH-рассылка отправлена. Получателей: {sent}.")
        except Exception:
            pass


async def activate_banner_from_event(ev: dict):
    media_files = ev.get("media_files") or []
    if not media_files:
        return
    f = media_files[0]
    b_media = {"type": f.get("type"), "file_id": f.get("file_id")}

    parts = []
    if ev.get("title"):
        parts.append(sanitize(ev["title"]))
    if ev.get("description"):
        parts.append(sanitize(ev["description"]))
    b_text = "\n\n".join(parts) if parts else None

    b_link = ev.get("contact")
    lat = ev.get("lat")
    lon = ev.get("lon")
    days = 1  # по умолчанию 1 день баннера при включении опции

    banners = _load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    expire = datetime.now() + timedelta(days=days)
    banners.append({
        "id": new_id,
        "owner": ev["author"],
        "media_type": b_media["type"],
        "file_id": b_media["file_id"],
        "text": b_text,
        "link": b_link,
        "lat": lat,
        "lon": lon,
        "expire": expire.isoformat(),
        "notified": False
    })
    _save_banners(banners)


# ===================== УДАЛЕНИЕ СОБЫТИЯ =====================

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
        return await cq.answer("Вы не являетесь автором этого объявления.", show_alert=True)

    target["expire"] = datetime.now().isoformat()
    _save_events(events)

    await cq.answer("Событие удалено.")
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ===================== ПОИСК СОБЫТИЙ =====================

@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message, state: FSMContext):
    await state.set_state(SearchEvents.menu)
    await m.answer(
        "Что ищем? Выбери раздел:\n"
        "🔎 Все события — обычные встречи, вечеринки, спорт и т.п.\n"
        "🛒 Маркет — объявления «Куплю/Продам» рядом.\n"
        "💼 Работа — вакансии и те, кто ищет работу.\n"
        "✨ Покажи себя — анкеты и самопрезентации.\n"
        "🔍 Ищу тебя — поиск людей и пропавших питомцев.",
        reply_markup=kb_search_menu()
    )


@dp.message(SearchEvents.menu)
async def search_menu_router(m: Message, state: FSMContext):
    text = m.text or ""
    if text == "⬅ Назад":
        await state.clear()
        return await m.answer("Возвращаю в главное меню ☺️", reply_markup=kb_main())

    mapping = {
        "🔎 Все события рядом": SearchEvents.all,
        "🛒 Маркет": SearchEvents.market,
        "💼 Работа": SearchEvents.work,
        "✨ Покажи себя": SearchEvents.selfpromo,
        "🔍 Ищу тебя": SearchEvents.findyou,
    }
    target_state = mapping.get(text)
    if not target_state:
        return await m.answer("Пожалуйста, выбери вариант из списка ☺️", reply_markup=kb_search_menu())

    await state.set_state(target_state)
    await m.answer(
        "📍 Отправь геолокацию (скрепка → Геопозиция → точка на карте),\n"
        f"я покажу подходящие объявления в радиусе ~ {DEFAULT_RADIUS_KM} км.",
        reply_markup=kb_send_location()
    )


async def _search_and_show(m: Message, user_loc, category_filter: str, state: FSMContext):
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
        if category_filter == "all":
            pass
        elif category_filter == "market":
            if cat not in ("🛒 Куплю", "💰 Продам"):
                continue
        elif category_filter == "work":
            if cat not in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
                continue
        elif category_filter == "selfpromo":
            if cat != "✨ Покажи себя":
                continue
        elif category_filter == "findyou":
            if cat != "🔍 Ищу тебя":
                continue

        dist = geodesic(user_loc, (ev["lat"], ev["lon"])).km
        if dist <= DEFAULT_RADIUS_KM:
            found.append((ev, dist))

    # Сортировка: сначала ТОП (по дате оплаты), потом по расстоянию
    def _sort_key(item):
        ev, dist = item
        if ev.get("is_top"):
            paid_dt = _safe_dt(ev.get("top_paid_at")) or _safe_dt(ev.get("created")) or datetime.min
            return (0, -paid_dt.timestamp(), dist)
        return (1, dist, 0)

    found.sort(key=_sort_key)
    await state.clear()

    if not found:
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="➕ Создать событие")],
                [KeyboardButton(text="⬅ Назад")]
            ],
            resize_keyboard=True
        )
        return await m.answer("😔 Ничего рядом не найдено.\nМожешь создать своё объявление!", reply_markup=kb)

    for ev, dist in found:
        try:
            await send_event_media(m.chat.id, ev, with_distance=dist)
        except Exception:
            txt = format_event_card(ev, with_distance=dist)
            await m.answer(txt)


@dp.message(SearchEvents.all, F.location)
async def search_all_loc(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "all", state)


@dp.message(SearchEvents.market, F.location)
async def search_market_loc(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "market", state)


@dp.message(SearchEvents.work, F.location)
async def search_work_loc(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "work", state)


@dp.message(SearchEvents.selfpromo, F.location)
async def search_selfpromo_loc(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "selfpromo", state)


@dp.message(SearchEvents.findyou, F.location)
async def search_findyou_loc(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "findyou", state)


# ===================== БАННЕРЫ: ОТДЕЛЬНАЯ ПОКУПКА =====================

@dp.message(Command("banner"))
async def start_banner_flow(m: Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer(
        "🖼 Создание баннера.\n"
        "Отправь фото или видео для баннера.",
        reply_markup=kb_media_step()
    )


@dp.message(AddBanner.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def banner_media(m: Message, state: FSMContext):
    media = None
    if m.photo:
        media = {"type": "photo", "file_id": m.photo[-1].file_id}
    elif m.video:
        media = {"type": "video", "file_id": m.video.file_id}
    if not media:
        return await m.answer("Отправь фото или видео для баннера.", reply_markup=kb_media_step())
    await state.update_data(b_media=media)
    await state.set_state(AddBanner.description)
    await m.answer("✏ Введи текст для баннера:", reply_markup=kb_back())


@dp.message(AddBanner.media)
async def banner_media_other(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await m.answer("Отправь фото или видео для баннера.", reply_markup=kb_media_step())


@dp.message(AddBanner.description)
async def banner_desc(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.media)
        return await m.answer("Отправь фото или видео для баннера.", reply_markup=kb_media_step())
    await state.update_data(b_text=sanitize(m.text))
    await state.set_state(AddBanner.link)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())


@dp.message(AddBanner.link)
async def banner_link(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.description)
        return await m.answer("✏ Введи текст для баннера:", reply_markup=kb_back())
    if m.text.lower().strip() != "пропустить":
        await state.update_data(b_link=sanitize(m.text))
    else:
        await state.update_data(b_link=None)
    await state.set_state(AddBanner.duration)
    await m.answer("⏳ Выбери срок показа баннера:", reply_markup=kb_banner_duration())


@dp.message(AddBanner.duration)
async def banner_duration(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.link)
        return await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())
    if m.text not in BANNER_DURATIONS:
        return await m.answer("Выбери один из вариантов:", reply_markup=kb_banner_duration())
    days, amount = BANNER_DURATIONS[m.text]
    await state.update_data(b_days=days, _pay_uuid=None)
    await state.set_state(AddBanner.payment)
    await m.answer(
        f"📅 Срок: {days} дн.\n"
        f"💵 Стоимость: ${amount}\n\n"
        "Нажми «💳 Получить ссылку на оплату».",
        reply_markup=kb_payment()
    )


@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("⏳ Выбери срок показа баннера:", reply_markup=kb_banner_duration())


@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_pay_link(m: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("b_days")
    if not days:
        return await m.answer("❌ Срок не выбран.", reply_markup=kb_banner_duration())

    # Цена по дням
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
        return await m.answer("⚠ Не удалось получить ссылку. Проверь ключи.", reply_markup=kb_payment())

    pay = _load_payments()
    pay[uuid] = {"type": "banner_buy", "user_id": m.from_user.id, "payload": data}
    _save_payments(pay)

    await state.update_data(_pay_uuid=uuid)
    await m.answer(
        f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажми «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("❌ Счёт не найден. Получи ссылку ещё раз.", reply_markup=kb_payment())

    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подожди минуту и попробуй снова.", reply_markup=kb_payment())

    d = await state.get_data()
    media = d.get("b_media")
    if not media:
        return await m.answer("❌ Медиа не найдено. Начни заново.", reply_markup=kb_main())

    text = d.get("b_text")
    link = d.get("b_link")
    days = d.get("b_days", 1)

    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    last_loc = (u.get("last_location") or {})
    lat = last_loc.get("lat")
    lon = last_loc.get("lon")

    banners = _load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    expire = datetime.now() + timedelta(days=days)
    banners.append({
        "id": new_id,
        "owner": m.from_user.id,
        "media_type": media["type"],
        "file_id": media["file_id"],
        "text": text,
        "link": link,
        "lat": lat,
        "lon": lon,
        "expire": expire.isoformat(),
        "notified": False
    })
    _save_banners(banners)

    await state.clear()
    await m.answer("✅ Баннер активирован и будет показываться пользователям.", reply_markup=kb_main())


# ===================== ИЗБРАННОЕ =====================

@dp.callback_query(F.data.startswith("fav_add:"))
async def cb_fav_add(cq: CallbackQuery):
    try:
        _, ev_id_str = cq.data.split(":", 1)
        ev_id = int(ev_id_str)
    except Exception:
        return await cq.answer("Что-то пошло не так 🙈", show_alert=True)

    events = _load_events()
    now = datetime.now()
    ev = next(
        (e for e in events
         if e.get("id") == ev_id and _safe_dt(e.get("expire")) and _safe_dt(e["expire"]) > now),
        None
    )
    if not ev:
        return await cq.answer("Это событие уже недоступно 🕒", show_alert=True)

    users = _load_users()
    uid = str(cq.from_user.id)
    u = users.get(uid) or {}
    favs = u.get("favorites") or []

    if ev_id in favs:
        return await cq.answer("Уже в избранном ⭐", show_alert=False)

    favs.append(ev_id)
    u["favorites"] = favs
    users[uid] = u
    _save_users(users)

    await cq.answer("Добавил в избранное ✨", show_alert=False)


@dp.message(F.text == "⭐ Избранное")
async def show_favorites(m: Message):
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    fav_ids = u.get("favorites") or []

    if not fav_ids:
        return await m.answer(
            "У тебя пока нет избранных событий ⭐\n"
            "Нажми «⭐ В избранное» под любым событием, чтобы сохранить его.",
            reply_markup=kb_main()
        )

    events = _load_events()
    now = datetime.now()
    fav_events = [
        e for e in events
        if e.get("id") in fav_ids and _safe_dt(e.get("expire")) and _safe_dt(e["expire"]) > now
    ]

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
            txt = format_event_card(ev)
            await m.answer(txt)

    await m.answer(
        "Готово 🙌\nЕсли событие истекает — оно исчезает и из избранного автоматически.",
        reply_markup=kb_main()
    )


# ===================== ДЕЙМОН ПРОДЛЕНИЯ / ИСТЕЧЕНИЯ =====================

async def push_daemon():
    """Пуш за 2 часа до окончания событий и баннеров, снятие истёкшего ТОПа и предложения продлить."""
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
                        await bot.send_message(ev["author"],
                                               f"⏳ Событие «{ev['title']}» скоро завершится. Продлить?",
                                               reply_markup=kb)
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
                        await bot.send_message(b["owner"],
                                               "⏳ Срок показа баннера заканчивается. Продлить?",
                                               reply_markup=kb)
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
        amount = EXTEND_TARIFFS_USD.get(hours)
        if not amount:
            return await cq.answer("Тариф не найден", show_alert=True)
        order_id = f"extend_event_{ev_id}_{cq.from_user.id}_{hours}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar event extend {hours}h")
        if not link or not uuid:
            return await cq.answer("Не удалось создать счёт", show_alert=True)
        pay = _load_payments()
        pay[uuid] = {"type": "event_extend", "user_id": cq.from_user.id,
                     "payload": {"event_id": ev_id, "hours": hours}}
        _save_payments(pay)
        await cq.message.answer(
            f"💳 Ссылка на оплату продления:\n{link}\n\nПосле оплаты нажми «✅ Я оплатил».",
        )
        await cq.answer()
    except Exception:
        await cq.answer("Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("extend_bn:"))
async def cb_extend_banner(cq: CallbackQuery):
    try:
        _, b_id, days = cq.data.split(":")
        b_id = int(b_id)
        days = int(days)
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
        pay[uuid] = {"type": "banner_extend", "user_id": cq.from_user.id,
                     "payload": {"banner_id": b_id, "days": days}}
        _save_payments(pay)
        await cq.message.answer(
            f"💳 Ссылка на оплату продления баннера:\n{link}\n\nПосле оплаты нажми «✅ Я оплатил».",
        )
        await cq.answer()
    except Exception:
        await cq.answer("Ошибка", show_alert=True)


# ===================== ОБРАБОТКА CALLBACK ОПЛАТ (WEBHOOK ОТ ПЛАТЁЖКИ) =====================

async def handle_payment_callback(request: web.Request):
    """
    Эндпоинт для callbacks от платёжной системы (если настроишь).
    Сейчас просто логирует запрос.
    """
    try:
        data = await request.json()
        logging.info(f"PAYMENT CALLBACK: {data}")
    except Exception:
        logging.exception("Error parsing payment callback")
    return web.Response(text="OK")


# ===================== FALLBACK =====================

@dp.message()
async def fallback(m: Message):
    if not m.text:
        return
    await m.answer("Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())


# ===================== RUN APP (WEBHOOK, Render) =====================

async def make_web_app():
    app = web.Application()
    app.router.add_post("/payment_callback", handle_payment_callback)
    app.router.add_get("/payment_callback", handle_payment_callback)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    return app


if __name__ == "__main__":
    # Локальный запуск (long polling)
    async def main():
        asyncio.create_task(push_daemon())
        await dp.start_polling(bot)

    asyncio.run(main())
