# main.py
# PartyRadar — монолитный бот на Aiogram 3.x + webhook (Render)
# Требует: aiogram==3.x, aiohttp, python-dotenv, geopy, aiohttp

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import aiohttp
from aiohttp import web
from geopy.distance import geodesic

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command
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
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

# ===================== CONFIG =====================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

LOGO_URL = ""  # при желании можно указать URL логотипа

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("partyradar")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Файлы данных
EVENTS_FILE = "events.json"
USERS_FILE = "users.json"
BANNERS_FILE = "banners.json"
PAYMENTS_FILE = "payments.json"

DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2
MAX_ACTIVE_BANNERS = 3

# ===================== ТАРИФЫ =====================

PAID_OPTIONS = {
    "duration": {
        24:  {"label": "🕐 1 день (бесплатно)", "price": 0.0},
        72:  {"label": "⏱ 3 дня — $0.90", "price": 0.90},
        168: {"label": "⏱ 7 дней — $1.90", "price": 1.90},
        720: {"label": "⏱ 30 дней — $4.90", "price": 4.90},
    },
    "top":    {"label": "⭐ ТОП — $1.50",        "price": 1.50},
    "banner": {"label": "🖼 Баннер — $7.00",     "price": 7.00},
    "push":   {"label": "📣 PUSH 30 км — $1.50", "price": 1.50},
}

# Тарифы продления событий (используются в пуш-уведомлениях)
EXTEND_TARIFFS_USD = {
    24: 1.0,
    72: 3.0,
    168: 5.0,
    720: 6.0,
}

# Сроки баннеров
BANNER_DURATIONS = {
    "📅 1 день — $7":   (1, 7.0),
    "📅 3 дня — $15":  (3, 15.0),
    "📅 7 дней — $30":  (7, 30.0),
    "📅 15 дней — $45": (15, 45.0),
    "📅 30 дней — $75": (30, 75.0),
}

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


def _save_events(events: List[dict]):
    _save_json(EVENTS_FILE, events)


def _load_users() -> Dict[str, dict]:
    return _load_json(USERS_FILE, {})


def _save_users(users: Dict[str, dict]):
    _save_json(USERS_FILE, users)


def _load_banners() -> List[dict]:
    return _load_json(BANNERS_FILE, [])


def _save_banners(banners: List[dict]):
    _save_json(BANNERS_FILE, banners)


def _load_payments() -> Dict[str, dict]:
    data = _load_json(PAYMENTS_FILE, {})
    if isinstance(data, list):
        data = {}
    return data


def _save_payments(payments: Dict[str, dict]):
    _save_json(PAYMENTS_FILE, payments)


def _safe_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

# ===================== CRYPTOCLOUD =====================

async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
        logger.warning("CryptoCloud не настроен (нет API ключа/SHOP_ID)")
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
        payments[uuid] = {
            "type": "generic",
            "user_id": None,
            "amount": amount_usd,
            "description": description,
            "order_id": order_id,
            "created": datetime.now().isoformat(),
        }
        _save_payments(payments)
        return link, uuid
    except Exception as e:
        logger.exception(f"Ошибка создания счёта CryptoCloud: {e}")
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
        inv = result[0]
        status = (inv.get("status") or "").lower()
        return status in ("paid", "overpaid")
    except Exception as e:
        logger.exception(f"Ошибка проверки оплаты CryptoCloud: {e}")
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
    constructor = State()
    payment = State()


class SearchEvents(StatesGroup):
    menu = State()
    all = State()
    market = State()
    work = State()
    selfpromo = State()
    findyou = State()


class AddBanner(StatesGroup):
    media = State()
    description = State()
    link = State()
    duration = State()
    payment = State()

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


def kb_search_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Все события рядом")],
            [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="💼 Работа")],
            [KeyboardButton(text="✨ Покажи себя"), KeyboardButton(text="🔍 Ищу тебя")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_send_location():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 1 день — $7"), KeyboardButton(text="📅 3 дня — $15")],
            [KeyboardButton(text="📅 7 дней — $30"), KeyboardButton(text="📅 15 дней — $45")],
            [KeyboardButton(text="📅 30 дней — $75")],
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


def kb_constructor(summary: str):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PAID_OPTIONS["duration"][24]["label"])],
            [KeyboardButton(text=PAID_OPTIONS["duration"][72]["label"])],
            [KeyboardButton(text=PAID_OPTIONS["duration"][168]["label"])],
            [KeyboardButton(text=PAID_OPTIONS["duration"][720]["label"])],
            [KeyboardButton(text="⭐ ТОП (переключить)")],
            [KeyboardButton(text="🖼 Баннер (переключить)")],
            [KeyboardButton(text="📣 PUSH 30 км (переключить)")],
            [KeyboardButton(text="👁 Предпросмотр объявления")],
            [KeyboardButton(text="💥 Оплатить и опубликовать")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True,
        input_field_placeholder=(summary[:64] if summary else None)
    )

# ===================== HELPERS =====================

def sanitize(text: str) -> str:
    return re.sub(r"[^\S\r\n]+", " ", text or "").strip()


def format_event(ev: dict, dist_km: Optional[float] = None) -> str:
    parts = [f"📌 <b>{sanitize(ev.get('title') or '')}</b>"]
    cat = ev.get("category")
    if cat:
        parts.append(f"📍 {sanitize(cat)}")
    if ev.get("description"):
        parts.append(f"📝 {sanitize(ev['description'])}")
    if ev.get("datetime"):
        dt = _safe_dt(ev["datetime"])
        if dt:
            parts.append(f"📅 {dt.strftime('%d.%m.%Y %H:%M')}")
    if ev.get("price"):
        parts.append(f"💵 Цена: {sanitize(ev['price'])}")
    if ev.get("contact"):
        parts.append(f"☎ Контакт: {sanitize(ev['contact'])}")
    if dist_km is not None:
        parts.append(f"📏 Расстояние: {dist_km:.1f} км")
    if ev.get("is_top"):
        parts.append("🔥 <b>ТОП</b>")
    return "\n".join(parts)


async def send_event_message(chat_id: int, ev: dict, dist_km: Optional[float] = None, preview: bool = False):
    text = format_event(ev, dist_km)
    buttons = []

    if ev.get("lat") is not None and ev.get("lon") is not None:
        gmap = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        buttons.append([InlineKeyboardButton(text="🌐 Открыть в Google Maps", url=gmap)])

    if not preview and ev.get("id") is not None:
        row = [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add:{ev['id']}")]
        if ev.get("author") == chat_id:
            row.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ev_del:{ev['id']}"))
        buttons.append(row)

    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

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
            else:
                group.append(InputMediaVideo(media=f["file_id"], caption=caption, parse_mode="HTML"))
        await bot.send_media_group(chat_id, group)
        if markup:
            await bot.send_message(chat_id, " ", reply_markup=markup)
    elif len(media) == 1:
        f = media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=markup)
        else:
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)

# ===================== МОДЕРАЦИЯ =====================

FORBIDDEN_KEYWORDS = [
    "интим", "эскорт", "sex", "секс услуги", "наркотик", "закладка",
    "оружие", "казино", "1xbet", "онлифанс", "onlyfans", "порн"
]

def check_moderation(text: str) -> bool:
    t = (text or "").lower()
    for w in FORBIDDEN_KEYWORDS:
        if w in t:
            return False
    return True

# ===================== ЛИМИТ БЕСПЛАТНЫХ ОБЪЯВЛЕНИЙ =====================

def can_use_free_in_category(user_id: int, category: str) -> bool:
    events = _load_events()
    now = datetime.now()
    cutoff = now - timedelta(hours=24)
    for ev in events:
        if int(ev.get("author") or 0) != user_id:
            continue
        if ev.get("category") != category:
            continue
        if not ev.get("is_free"):
            continue
        dt = _safe_dt(ev.get("created"))
        if dt and dt >= cutoff:
            return False
    return True

# ===================== START =====================

async def send_welcome(m: Message):
    # Лого, если есть
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

    text = (
        "👋 Добро пожаловать в <b>PartyRadar</b>!\n\n"
        "📍 Бот помогает находить и создавать события по геолокации:\n"
        "• вечеринки, встречи, свидания, спорт\n"
        "• маркет (куплю/продам)\n"
        "• работа (ищу / предлагаю)\n"
        "• «Покажи себя» — self promo / соцсети\n"
        "• «Ищу тебя» — поиск людей и питомцев\n"
        "• «Поздравления» — короткие объявления без даты\n\n"
        "🆓 1 бесплатное событие в сутки на каждую категорию.\n"
        "💎 Платные функции: срок, ТОП, баннер, PUSH по радиусу 30 км.\n\n"
        "Выбери действие из меню 👇"
    )
    await m.answer(text, reply_markup=kb_main())


@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await send_welcome(m)

# ===================== SUPPORT =====================

@dp.message(F.text == "📩 Связаться с нами")
async def support(m: Message):
    await m.answer(
        "💬 Предложения и вопросы:\n"
        "👉 <b>@drscorohod</b>",
        reply_markup=kb_main()
    )

# ===================== СОЗДАНИЕ СОБЫТИЯ =====================

@dp.message(F.text == "➕ Создать событие")
async def create_event(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer("📝 Введи название события:", reply_markup=kb_back())


@dp.message(AddEvent.title)
async def ev_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    title = sanitize(m.text)
    if not title:
        return await m.answer("Название не может быть пустым.", reply_markup=kb_back())
    await state.update_data(title=title)
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи описание события:", reply_markup=kb_back())


@dp.message(AddEvent.description)
async def ev_desc(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.title)
        return await m.answer("📝 Введи название события:", reply_markup=kb_back())
    descr = sanitize(m.text)
    if not check_moderation(descr):
        return await m.answer("Текст содержит запрещённый контент. Попробуй переформулировать.")
    await state.update_data(description=descr)
    await state.set_state(AddEvent.category)
    await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())


@dp.message(AddEvent.category)
async def ev_category(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer("🧾 Введи описание события:", reply_markup=kb_back())

    cat = sanitize(m.text)
    await state.update_data(category=cat)

    # описания рубрик
    if cat == "✨ Покажи себя":
        await m.answer("✨ Покажи себя — self promo, реклама себя и соцсетей. Опиши себя, добавь ссылки.")
    elif cat == "🔍 Ищу тебя":
        await m.answer("🔍 Ищу тебя — поиск людей и потерянных питомцев, опиши, кого ищешь.")
    elif cat == "🎊 Поздравления":
        await m.answer("🎊 Поздравления — короткие объявления без привязки к дате.")
    elif cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
        await m.answer("💼 Работа — «Ищу» и «Предлагаю». Можно без даты, главное — суть предложения.")

    # Маркет -> цена
    if cat in ("🛒 Куплю", "💰 Продам"):
        await state.set_state(AddEvent.dt_or_price)
        return await m.answer(
            "💵 Укажи цену (например: 150€, 200$, 5000₽ или «договорная»):",
            reply_markup=kb_back()
        )

    # Работа / Покажи себя / Ищу тебя / Поздравления — без даты
    if cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу", "✨ Покажи себя", "🔍 Ищу тебя", "🎊 Поздравления"):
        await state.update_data(datetime=None, price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "📎 Прикрепи до 3 фото/видео или сразу отправь геолокацию.\n"
            "⚠ Аудио и кружки не поддерживаются.",
            reply_markup=kb_media_step()
        )

    # остальные — с датой
    await state.set_state(AddEvent.dt_or_price)
    await m.answer(
        "📆 Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\nНапример: 25.12.2025 21:00",
        reply_markup=kb_back()
    )


@dp.message(AddEvent.dt_or_price)
async def ev_dt_price(m: Message, state: FSMContext):
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
                return await m.answer("Нельзя указать прошедшую дату/время.", reply_markup=kb_back())
        except ValueError:
            return await m.answer("Формат неверный. Пример: 25.12.2025 21:00", reply_markup=kb_back())
        await state.update_data(datetime=dt.isoformat(), price=None, media_files=[])

    await state.set_state(AddEvent.media)
    await m.answer(
        "📎 Прикрепи до 3 фото/видео или сразу отправь геолокацию.\n"
        "⚠ Аудио и кружки не поддерживаются.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def ev_media(m: Message, state: FSMContext):
    data = await state.get_data()
    media = data.get("media_files", [])
    if len(media) >= 3:
        return await m.answer("Уже добавлено 3 файла. Теперь отправь геолокацию.", reply_markup=kb_media_step())

    if m.photo:
        media.append({"type": "photo", "file_id": m.photo[-1].file_id})
    elif m.video:
        media.append({"type": "video", "file_id": m.video.file_id})
    await state.update_data(media_files=media)
    await m.answer(
        f"Файл добавлен ({len(media)}/3). Можно добавить ещё или отправить геолокацию.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.text == "⬅ Назад")
async def ev_media_back(m: Message, state: FSMContext):
    data = await state.get_data()
    media = data.get("media_files", [])
    cat = data.get("category")

    if media:
        media.pop()
        await state.update_data(media_files=media)
        return await m.answer(f"Удалён последний файл ({len(media)}/3).", reply_markup=kb_media_step())

    # если медиа нет, шагом назад будет дата/цена или категория
    if cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу", "✨ Покажи себя", "🔍 Ищу тебя", "🎊 Поздравления"):
        await state.set_state(AddEvent.category)
        return await m.answer("Вернулись к выбору категории:", reply_markup=kb_categories())
    else:
        await state.set_state(AddEvent.dt_or_price)
        return await m.answer("Вернулись к вводу даты/цены:", reply_markup=kb_back())


@dp.message(AddEvent.media, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE}))
async def ev_media_unsupported(m: Message, state: FSMContext):
    await m.answer("Аудио и кружки не поддерживаются. Прикрепи фото или видео.", reply_markup=kb_media_step())


@dp.message(AddEvent.media, F.location)
async def ev_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    u["last_location"] = {"lat": m.location.latitude, "lon": m.location.longitude}
    u["last_seen"] = datetime.now().isoformat()
    users[str(m.from_user.id)] = u
    _save_users(users)
    await state.set_state(AddEvent.location)
    await m.answer("Геолокация получена ✅\nТеперь укажи контакт (или напиши «Пропустить»).", reply_markup=kb_back())


@dp.message(AddEvent.location)
async def ev_contact_step(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer("Вернулись к медиа/геолокации.", reply_markup=kb_media_step())
    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=sanitize(m.text))
    else:
        await state.update_data(contact=None)

    # Переход к конструктору платных опций
    await state.set_state(AddEvent.constructor)
    data = await state.get_data()
    summary = await build_constructor_summary(m.from_user.id, data)
    await m.answer(
        "⚙️ Конструктор платных опций.\n"
        "Выбери срок размещения, ТОП, баннер, PUSH.\n"
        "Одна оплата = сумма выбранных опций.\n\n" + summary,
        reply_markup=kb_constructor(summary)
    )

# ===================== КОНСТРУКТОР ПЛАТНЫХ ОПЦИЙ =====================

async def build_constructor_summary(user_id: int, data: dict) -> str:
    cat = data.get("category")
    duration_hours = data.get("duration_hours", 24)
    opt_top = data.get("opt_top", False)
    opt_banner = data.get("opt_banner", False)
    opt_push = data.get("opt_push", False)

    free_allowed = can_use_free_in_category(user_id, cat) if cat else False
    dur_cfg = PAID_OPTIONS["duration"][duration_hours]

    if duration_hours == 24 and not free_allowed:
        dur_line = "🕐 Срок: 1 день — лимит бесплатных объявлений в этой категории на сегодня исчерпан."
        dur_price = 0.0
    else:
        dur_line = f"🕐 Срок: {dur_cfg['label']}"
        dur_price = dur_cfg["price"]

    top_line = "⭐ ТОП: включен" if opt_top else "⭐ ТОП: выключен"
    banner_line = "🖼 Баннер: включен" if opt_banner else "🖼 Баннер: выключен"
    push_line = "📣 PUSH 30 км: включен" if opt_push else "📣 PUSH 30 км: выключен"

    total = 0.0
    if duration_hours != 24:
        total += dur_price
    else:
        if free_allowed:
            dur_price = 0.0
        else:
            dur_price = 0.0  # не считаем, просто блокируем выбор

    if opt_top:
        total += PAID_OPTIONS["top"]["price"]
    if opt_banner:
        total += PAID_OPTIONS["banner"]["price"]
    if opt_push:
        total += PAID_OPTIONS["push"]["price"]

    total_line = f"💰 Итог: ${total:.2f}" + (" (бесплатно)" if total == 0 else "")
    return "\n".join([dur_line, top_line, banner_line, push_line, total_line])


@dp.message(AddEvent.constructor)
async def ev_constructor(m: Message, state: FSMContext):
    data = await state.get_data()
    txt = m.text or ""

    if txt == "⬅ Назад":
        await state.set_state(AddEvent.location)
        return await m.answer("Вернулись к вводу контакта.", reply_markup=kb_back())

    # выбор длительности
    for hours, cfg in PAID_OPTIONS["duration"].items():
        if txt == cfg["label"]:
            if hours == 24 and not can_use_free_in_category(m.from_user.id, data.get("category")):
                await m.answer(
                    "Лимит бесплатных объявлений в этой категории уже исчерпан.\n"
                    "Выбери платный срок: 3, 7 или 30 дней."
                )
            else:
                await state.update_data(duration_hours=hours)
            summary = await build_constructor_summary(m.from_user.id, await state.get_data())
            return await m.answer("Обновлённые настройки:\n\n" + summary, reply_markup=kb_constructor(summary))

    if txt == "⭐ ТОП (переключить)":
        await state.update_data(opt_top=not data.get("opt_top", False))
    elif txt == "🖼 Баннер (переключить)":
        await state.update_data(opt_banner=not data.get("opt_banner", False))
    elif txt == "📣 PUSH 30 км (переключить)":
        await state.update_data(opt_push=not data.get("opt_push", False))
    elif txt == "👁 Предпросмотр объявления":
        return await show_preview(m, state)
    elif txt == "💥 Оплатить и опубликовать":
        return await pay_or_publish(m, state)
    else:
        summary = await build_constructor_summary(m.from_user.id, data)
        return await m.answer("Выбери опцию из меню конструктора.", reply_markup=kb_constructor(summary))

    summary = await build_constructor_summary(m.from_user.id, await state.get_data())
    await m.answer("Обновлённые настройки:\n\n" + summary, reply_markup=kb_constructor(summary))


async def show_preview(m: Message, state: FSMContext):
    data = await state.get_data()
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
        "media_files": data.get("media_files", []),
        "contact": data.get("contact"),
        "is_top": data.get("opt_top", False),
    }
    await m.answer("Предпросмотр объявления:")
    await send_event_message(m.chat.id, ev, preview=True)
    summary = await build_constructor_summary(m.from_user.id, data)
    await m.answer("Если всё ок — жми «💥 Оплатить и опубликовать».\n\n" + summary,
                   reply_markup=kb_constructor(summary))


async def pay_or_publish(m: Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("title") or ""
    desc = data.get("description") or ""
    full_text = f"{title}\n{desc}"
    if not check_moderation(full_text):
        await state.clear()
        return await m.answer("Объявление не прошло модерацию. Попробуй переписать текст.", reply_markup=kb_main())

    duration_hours = data.get("duration_hours", 24)
    cat = data.get("category")
    free_allowed = can_use_free_in_category(m.from_user.id, cat) if cat else False

    if duration_hours == 24 and not free_allowed:
        summary = await build_constructor_summary(m.from_user.id, data)
        return await m.answer(
            "Лимит бесплатных объявлений в этой категории на сегодня уже использован.\n"
            "Выбери платный срок размещения.",
            reply_markup=kb_constructor(summary)
        )

    total = 0.0
    if duration_hours != 24:
        total += PAID_OPTIONS["duration"][duration_hours]["price"]
    if data.get("opt_top"):
        total += PAID_OPTIONS["top"]["price"]
    if data.get("opt_banner"):
        total += PAID_OPTIONS["banner"]["price"]
    if data.get("opt_push"):
        total += PAID_OPTIONS["push"]["price"]

    if total <= 0:
        await publish_event(m, data, duration_hours, is_free=True)
        await state.clear()
        return await m.answer("Событие опубликовано бесплатно ✅", reply_markup=kb_main())

    await state.set_state(AddEvent.payment)
    await state.update_data(payment_total=total)
    await m.answer(
        f"К оплате: ${total:.2f}\n"
        "Нажми «💳 Получить ссылку на оплату», оплати и затем «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddEvent.payment, F.text == "⬅ Назад")
async def ev_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.constructor)
    summary = await build_constructor_summary(m.from_user.id, await state.get_data())
    await m.answer("Вернулись к конструктору опций.\n\n" + summary, reply_markup=kb_constructor(summary))


@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def ev_pay_link(m: Message, state: FSMContext):
    data = await state.get_data()
    total = data.get("payment_total", 0.0)
    if total <= 0:
        return await m.answer("Сумма к оплате не определена. Попробуй вернуться и настроить опции заново.",
                              reply_markup=kb_constructor(await build_constructor_summary(m.from_user.id, data)))

    order_id = f"event_pack_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(total, order_id, "PartyRadar: пакет опций для события")
    if not link or not uuid:
        return await m.answer("Не удалось создать счёт. Проверь конфиг CryptoCloud.", reply_markup=kb_payment())

    payments = _load_payments()
    payments[uuid] = {
        "type": "event_pack",
        "user_id": m.from_user.id,
        "payload": data,
        "created": datetime.now().isoformat(),
    }
    _save_payments(payments)

    await state.update_data(_pay_uuid=uuid)

    await m.answer(
        f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты вернись в бот и нажми «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddEvent.payment, F.text == "✅ Я оплатил")
async def ev_pay_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("Счёт не найден. Нажми «💳 Получить ссылку на оплату» ещё раз.", reply_markup=kb_payment())
    await m.answer("Проверяю оплату...")
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("Оплата пока не подтверждена. Попробуй чуть позже.", reply_markup=kb_payment())

    await m.answer("Оплата подтверждена ✅ Публикую событие...")
    hours = data.get("duration_hours", 24)
    await publish_event(m, data, hours, is_free=(hours == 24))
    await state.clear()
    await m.answer("Событие опубликовано ✅", reply_markup=kb_main())


async def publish_event(m: Message, data: dict, hours: int, is_free: bool):
    events = _load_events()
    now = datetime.now()
    expires = now + timedelta(hours=hours)
    new_id = (events[-1]["id"] + 1) if events else 1

    media = data.get("media_files") or []
    if not media:
        # подставляем логотип, если есть
        for ext in ("png", "jpg", "jpeg"):
            p = f"logo.{ext}"
            if os.path.exists(p):
                media = [{"type": "photo", "file_id": p, "is_local": True}]
                break

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
        "media_files": media,
        "contact": data.get("contact"),
        "created": now.isoformat(),
        "expire": expires.isoformat(),
        "is_free": bool(is_free),
        "is_top": data.get("opt_top", False),
        "top_expire": expires.isoformat() if data.get("opt_top") else None,
        "top_paid_at": now.isoformat() if data.get("opt_top") else None,
        "notified": False,
    }
    events.append(ev)
    _save_events(events)

    # отправляем автору
    await send_event_message(m.chat.id, ev)

    # баннер из события, если включен
    if data.get("opt_banner"):
        await activate_banner_from_event(ev)

    # PUSH
    if data.get("opt_push"):
        sent = await send_push_for_event(ev)
        try:
            await m.answer(f"📣 PUSH отправлен {sent} пользователям в радиусе {DEFAULT_RADIUS_KM} км.")
        except Exception:
            pass


async def activate_banner_from_event(ev: dict):
    media = (ev.get("media_files") or [])
    if not media:
        return
    f = media[0]
    text_parts = []
    if ev.get("title"):
        text_parts.append(sanitize(ev["title"]))
    if ev.get("description"):
        text_parts.append(sanitize(ev["description"]))
    text = "\n\n".join(text_parts) if text_parts else None

    banners = _load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    expire = datetime.now() + timedelta(days=1)  # базово 1 день баннера при опции
    banners.append({
        "id": new_id,
        "owner": ev["author"],
        "media_type": f["type"],
        "file_id": f["file_id"],
        "text": text,
        "link": ev.get("contact"),
        "lat": ev.get("lat"),
        "lon": ev.get("lon"),
        "expire": expire.isoformat(),
        "notified": False,
    })
    _save_banners(banners)


async def send_push_for_event(ev: dict) -> int:
    lat = ev.get("lat")
    lon = ev.get("lon")
    if lat is None or lon is None:
        return 0
    users = _load_users()
    cnt = 0
    for uid, info in users.items():
        loc = info.get("last_location") or {}
        u_lat = loc.get("lat")
        u_lon = loc.get("lon")
        if u_lat is None or u_lon is None:
            continue
        dist = geodesic((u_lat, u_lon), (lat, lon)).km
        if dist <= DEFAULT_RADIUS_KM:
            try:
                await send_event_message(int(uid), ev, dist_km=dist)
                cnt += 1
            except Exception as e:
                logger.exception(f"Ошибка PUSH пользователю {uid}: {e}")
    return cnt

# ===================== УДАЛЕНИЕ СОБЫТИЯ =====================

@dp.callback_query(F.data.startswith("ev_del:"))
async def cb_delete_event(cq: CallbackQuery):
    try:
        _, ev_id_str = cq.data.split(":", 1)
        ev_id = int(ev_id_str)
    except Exception:
        return await cq.answer("Ошибка ID события.", show_alert=True)

    events = _load_events()
    ev = next((e for e in events if e.get("id") == ev_id), None)
    if not ev:
        return await cq.answer("Событие уже удалено.", show_alert=True)
    if int(ev.get("author") or 0) != cq.from_user.id:
        return await cq.answer("Ты не автор этого события.", show_alert=True)

    ev["expire"] = datetime.now().isoformat()
    _save_events(events)
    await cq.answer("Событие скрыто (удалено).", show_alert=False)
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

# ===================== ИЗБРАННОЕ =====================

@dp.callback_query(F.data.startswith("fav_add:"))
async def cb_fav_add(cq: CallbackQuery):
    try:
        _, ev_id_str = cq.data.split(":", 1)
        ev_id = int(ev_id_str)
    except Exception:
        return await cq.answer("Ошибка ID.", show_alert=True)

    events = _load_events()
    now = datetime.now()
    ev = next((e for e in events if e.get("id") == ev_id and _safe_dt(e.get("expire")) and _safe_dt(e["expire"]) > now), None)
    if not ev:
        return await cq.answer("Событие уже недоступно.", show_alert=True)

    users = _load_users()
    u = users.get(str(cq.from_user.id)) or {}
    fav = u.get("favorites") or []
    if ev_id in fav:
        return await cq.answer("Уже в избранном.", show_alert=False)
    fav.append(ev_id)
    u["favorites"] = fav
    users[str(cq.from_user.id)] = u
    _save_users(users)
    await cq.answer("Добавлено в избранное ⭐", show_alert=False)


@dp.message(F.text == "⭐ Избранное")
async def favorites(m: Message):
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    fav = u.get("favorites") or []
    if not fav:
        return await m.answer("У тебя пока нет избранных событий.", reply_markup=kb_main())

    events = _load_events()
    now = datetime.now()
    evs = [e for e in events if e.get("id") in fav and _safe_dt(e.get("expire")) and _safe_dt(e["expire"]) > now]
    if not evs:
        u["favorites"] = []
        users[str(m.from_user.id)] = u
        _save_users(users)
        return await m.answer("Избранные события истекли по времени.", reply_markup=kb_main())

    await m.answer("Твои избранные события:")
    for ev in evs:
        await send_event_message(m.chat.id, ev)
    await m.answer("Конец списка избранного.", reply_markup=kb_main())

# ===================== ПОИСК СОБЫТИЙ =====================

@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message, state: FSMContext):
    await state.set_state(SearchEvents.menu)
    await m.answer(
        "Что ищем?\n"
        "🔎 Все события рядом\n"
        "🛒 Маркет\n"
        "💼 Работа\n"
        "✨ Покажи себя\n"
        "🔍 Ищу тебя",
        reply_markup=kb_search_menu()
    )


@dp.message(SearchEvents.menu)
async def search_menu(m: Message, state: FSMContext):
    txt = m.text or ""
    if txt == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    mapping = {
        "🔎 Все события рядом": "all",
        "🛒 Маркет": "market",
        "💼 Работа": "work",
        "✨ Покажи себя": "selfpromo",
        "🔍 Ищу тебя": "findyou",
    }
    key = mapping.get(txt)
    if not key:
        return await m.answer("Выбери вариант из меню.", reply_markup=kb_search_menu())
    await state.update_data(search_mode=key)
    await m.answer(
        "Отправь геолокацию.\n\n"
        "📎 Скрепка → Геопозиция → Точка на карте.",
        reply_markup=kb_send_location()
    )


@dp.message(SearchEvents.menu, F.location)
async def search_loc_router(m: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("search_mode", "all")
    await search_with_location(m, state, (m.location.latitude, m.location.longitude), mode)


async def search_with_location(m: Message, state: FSMContext, loc: Tuple[float, float], mode: str):
    # обновим last_location пользователя
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    u["last_location"] = {"lat": loc[0], "lon": loc[1]}
    u["last_seen"] = datetime.now().isoformat()
    users[str(m.from_user.id)] = u
    _save_users(users)

    events = _load_events()
    now = datetime.now()
    found: List[Tuple[dict, float]] = []
    for ev in events:
        exp = _safe_dt(ev.get("expire"))
        if not exp or exp <= now:
            continue
        if ev.get("lat") is None or ev.get("lon") is None:
            continue

        cat = ev.get("category")
        if mode == "market" and cat not in ("🛒 Куплю", "💰 Продам"):
            continue
        if mode == "work" and cat not in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
            continue
        if mode == "selfpromo" and cat != "✨ Покажи себя":
            continue
        if mode == "findyou" and cat != "🔍 Ищу тебя":
            continue
        # mode == all — любой категории

        dist = geodesic((ev["lat"], ev["lon"]), loc).km
        if dist <= DEFAULT_RADIUS_KM:
            found.append((ev, dist))

    # сортировка: сначала ТОП (по дате top_paid_at), потом расстояние
    def sort_key(item):
        ev, dist = item
        if ev.get("is_top"):
            top_dt = _safe_dt(ev.get("top_paid_at")) or _safe_dt(ev.get("created")) or datetime.min
            return (0, -top_dt.timestamp(), dist)
        return (1, dist, 0)

    found.sort(key=sort_key)
    await state.clear()

    if not found:
        return await m.answer("Ничего рядом не найдено. Попробуй позже или создай своё событие.",
                              reply_markup=kb_main())

    for ev, dist in found:
        await send_event_message(m.chat.id, ev, dist_km=dist)
    await m.answer("Конец списка.", reply_markup=kb_main())

# ===================== BANНЕРЫ (отдельный конструктор по команде) =====================

@dp.message(Command("banner"))
async def cmd_banner(m: Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer("Отправь фото или видео для баннера.", reply_markup=kb_media_step())


@dp.message(AddBanner.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def banner_media(m: Message, state: FSMContext):
    if m.photo:
        media = {"type": "photo", "file_id": m.photo[-1].file_id}
    else:
        media = {"type": "video", "file_id": m.video.file_id}
    await state.update_data(b_media=media)
    await state.set_state(AddBanner.description)
    await m.answer("Введи текст баннера:", reply_markup=kb_back())


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
    await m.answer("Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())


@dp.message(AddBanner.link)
async def banner_link(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.description)
        return await m.answer("Введи текст баннера:", reply_markup=kb_back())
    if m.text.lower().strip() != "пропустить":
        await state.update_data(b_link=sanitize(m.text))
    else:
        await state.update_data(b_link=None)
    await state.set_state(AddBanner.duration)
    await m.answer("Выбери срок показа баннера:", reply_markup=kb_banner_duration())


@dp.message(AddBanner.duration)
async def banner_duration(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.link)
        return await m.answer("Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())
    if m.text not in BANNER_DURATIONS:
        return await m.answer("Выбери срок из списка.", reply_markup=kb_banner_duration())
    days, amount = BANNER_DURATIONS[m.text]
    await state.update_data(b_days=days, b_amount=amount)
    await state.set_state(AddBanner.payment)
    await m.answer(
        f"Срок: {days} дн.\nК оплате: ${amount:.2f}\n"
        "Нажми «💳 Получить ссылку на оплату».",
        reply_markup=kb_payment()
    )


@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("Выбери срок показа баннера:", reply_markup=kb_banner_duration())


@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_pay_link(m: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("b_amount")
    days = data.get("b_days")
    if not amount or not days:
        return await m.answer("Срок/сумма не определены. Начни заново.", reply_markup=kb_main())

    order_id = f"banner_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link or not uuid:
        return await m.answer("Не удалось создать счёт.", reply_markup=kb_payment())

    payments = _load_payments()
    payments[uuid] = {
        "type": "banner",
        "user_id": m.from_user.id,
        "payload": data,
        "created": datetime.now().isoformat(),
    }
    _save_payments(payments)
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"Ссылка на оплату:\n{link}\nПосле оплаты нажми «✅ Я оплатил».", reply_markup=kb_payment())


@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_pay_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("Счёт не найден. Нажми «💳 Получить ссылку» ещё раз.", reply_markup=kb_payment())
    await m.answer("Проверяю оплату...")
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("Оплата пока не найдена. Попробуй позже.", reply_markup=kb_payment())

    media = data.get("b_media")
    if not media:
        return await m.answer("Медиа не найдено. Начни заново.", reply_markup=kb_main())
    text = data.get("b_text")
    link = data.get("b_link")
    days = data.get("b_days", 1)

    # попытка взять последнюю геолокацию пользователя
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    last_loc = u.get("last_location") or {}
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
        "notified": False,
    })
    _save_banners(banners)
    await state.clear()
    await m.answer("Баннер активирован и будет показываться пользователям.", reply_markup=kb_main())

# ===================== PUSH-DAEMON (напоминания о продлении) =====================

async def push_daemon():
    while True:
        try:
            now = datetime.now()

            # события
            events = _load_events()
            changed = False
            for ev in events:
                exp = _safe_dt(ev.get("expire"))
                if not exp:
                    continue
                # истечение ТОПа
                if ev.get("is_top") and ev.get("top_expire"):
                    te = _safe_dt(ev["top_expire"])
                    if te and te <= now:
                        ev["is_top"] = False
                        ev["top_expire"] = None
                        changed = True
                # уведомление за 2 часа до окончания
                if not ev.get("notified") and timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                    ev["notified"] = True
                    changed = True
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="📅 +1 день", callback_data=f"ext_ev:{ev['id']}:24")],
                            [InlineKeyboardButton(text="⏱ +3 дня", callback_data=f"ext_ev:{ev['id']}:72")],
                            [InlineKeyboardButton(text="⏱ +7 дней", callback_data=f"ext_ev:{ev['id']}:168")],
                            [InlineKeyboardButton(text="⏱ +30 дней", callback_data=f"ext_ev:{ev['id']}:720")],
                        ]
                    )
                    try:
                        await bot.send_message(ev["author"],
                                               f"Событие «{ev['title']}» скоро закончится. Продлить?",
                                               reply_markup=kb)
                    except Exception:
                        pass
            if changed:
                _save_events(events)

            # баннеры
            banners = _load_banners()
            b_changed = False
            for b in banners:
                exp = _safe_dt(b.get("expire"))
                if not exp or b.get("notified"):
                    continue
                if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                    b["notified"] = True
                    b_changed = True
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="+1 день", callback_data=f"ext_bn:{b['id']}:1")],
                            [InlineKeyboardButton(text="+3 дня", callback_data=f"ext_bn:{b['id']}:3")],
                            [InlineKeyboardButton(text="+7 дней", callback_data=f"ext_bn:{b['id']}:7")],
                            [InlineKeyboardButton(text="+30 дней", callback_data=f"ext_bn:{b['id']}:30")],
                        ]
                    )
                    try:
                        await bot.send_message(b["owner"], "Срок баннера заканчивается. Продлить?", reply_markup=kb)
                    except Exception:
                        pass
            if b_changed:
                _save_banners(banners)

        except Exception as e:
            logger.exception(f"push_daemon error: {e}")

        await asyncio.sleep(300)

@dp.callback_query(F.data.startswith("ext_ev:"))
async def cb_extend_event(cq: CallbackQuery):
    try:
        _, ev_id_str, hours_str = cq.data.split(":")
        ev_id = int(ev_id_str)
        hours = int(hours_str)
    except Exception:
        return await cq.answer("Ошибка параметров.", show_alert=True)
    amount = EXTEND_TARIFFS_USD.get(hours)
    if not amount:
        return await cq.answer("Тариф не найден.", show_alert=True)

    order_id = f"evext_{ev_id}_{hours}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"Продление события на {hours} ч.")
    if not link or not uuid:
        return await cq.answer("Не удалось создать счёт.", show_alert=True)

    payments = _load_payments()
    payments[uuid] = {
        "type": "event_extend",
        "user_id": cq.from_user.id,
        "payload": {"event_id": ev_id, "hours": hours},
        "created": datetime.now().isoformat(),
    }
    _save_payments(payments)
    await cq.message.answer(f"Ссылка на оплату продления:\n{link}")
    await cq.answer()


@dp.callback_query(F.data.startswith("ext_bn:"))
async def cb_extend_banner(cq: CallbackQuery):
    try:
        _, b_id_str, days_str = cq.data.split(":")
        b_id = int(b_id_str)
        days = int(days_str)
    except Exception:
        return await cq.answer("Ошибка параметров.", show_alert=True)

    # цена по BANNER_DURATIONS
    amount = None
    for label, (d, a) in BANNER_DURATIONS.items():
        if d == days:
            amount = a
            break
    if amount is None:
        return await cq.answer("Тариф не найден.", show_alert=True)

    order_id = f"bnext_{b_id}_{days}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"Продление баннера на {days} дн.")
    if not link or not uuid:
        return await cq.answer("Не удалось создать счёт.", show_alert=True)

    payments = _load_payments()
    payments[uuid] = {
        "type": "banner_extend",
        "user_id": cq.from_user.id,
        "payload": {"banner_id": b_id, "days": days},
        "created": datetime.now().isoformat(),
    }
    _save_payments(payments)
    await cq.message.answer(f"Ссылка на оплату продления баннера:\n{link}")
    await cq.answer()

# ===================== FALLBACK =====================

@dp.message()
async def fallback(m: Message):
    if not m.text:
        return
    await m.answer("Не понял сообщение. Используй кнопки ниже 👇", reply_markup=kb_main())

# ===================== WEBHOOK / RENDER =====================

async def on_startup(app: web.Application):
    # ставим webhook
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        webhook_url = f"https://{render_url}/webhook"
    else:
        # запасной вариант: если ты хочешь захардкодить домен:
        webhook_url = "https://partyradar.onrender.com/webhook"

    await bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

    # запускаем push_daemon
    app["push_daemon"] = asyncio.create_task(push_daemon())


async def on_shutdown(app: web.Application):
    task = app.get("push_daemon")
    if task:
        task.cancel()
    await bot.session.close()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot)
    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, host="0.0.0.0", port=port)
