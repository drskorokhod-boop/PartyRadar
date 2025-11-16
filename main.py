# main.py
# PartyRadar — финальная версия под Render
# Требует: aiogram==3.x, aiohttp, python-dotenv, geopy

import asyncio
import json
import logging
import os
import random
import re
import traceback
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
from aiohttp import web
from geopy.distance import geodesic
from aiogram import Bot, Dispatcher, F
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
    ContentType
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# ===================== CONFIG =====================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

# Если локального файла нет — можно указать прямой RAW-URL из GitHub (оставьте "" если не нужно)
LOGO_URL = ""  # например: "https://raw.githubusercontent.com/username/repo/branch/logo.png"

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
# Файлы хранения
EVENTS_FILE = "events.json"
BANNERS_FILE = "banners.json"
USERS_FILE = "users.json"
PAYMENTS_FILE = "payments.json"

# Радиусы, времена
DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2

# Баннеры: в показе максимум N
MAX_ACTIVE_BANNERS = 3

# Тарифы (USD)
PRICES = {
    "extend_48h": 1.0,
    "extend_week": 3.0,
    "extend_2week": 5.0,
    "top_week": 5.0,

    "banner_1d": 7.0,
    "banner_3d": 15.0,
    "banner_7d": 30.0,
    "banner_15d": 45.0,
    "banner_30d": 75.0,
}

# Цены для ТОП-продвижения
TOP_PRICES = {
    1: 1.5,
    3: 3.0,
    7: 6.0,
    15: 9.0,
    30: 15.0,
}

# Сроки жизни событий
LIFETIME_OPTIONS = {
    "🕐 1 день (бесплатно)": 24,
    "⏱ 3 дня": 72,
    "⏱ 7 дней": 168,
    "⏱ 30 дней": 720
}
TARIFFS_USD = {  # тарифы по сроку показа (в часах)
    24: 1.0,    # 1 день (используется только для продления)
    72: 1.5,    # 3 дня
    168: 3.0,   # 7 дней
    720: 6.0    # 30 дней
}

# Сроки баннеров
BANNER_DURATIONS = {
    "📆 1 день": (1, PRICES["banner_1d"]),
    "📆 3 дня": (3, PRICES["banner_3d"]),
    "📆 7 дней": (7, PRICES["banner_7d"]),
    "📆 15 дней": (15, PRICES["banner_15d"]),
    "📆 30 дней": (30, PRICES["banner_30d"]),
}

# ===================== STORAGE HELPERS =====================
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

# === AUTO CLEANUP OF EXPIRED BANNERS ===

import asyncio
from datetime import datetime

async def cleanup_banners():
    banners = _load_banners()
    now = datetime.utcnow().timestamp()

    # Фильтруем только актуальные баннеры
    active = [b for b in banners if b.get("expires_at", 0) > now]

    if len(active) != len(banners):
        removed = len(banners) - len(active)
        print(f"[CLEANUP] Удалено {removed} истёкших баннеров")
        _save_banners(active)

    return len(active)

async def banner_cleanup_scheduler():
    while True:
        try:
            await cleanup_banners()
        except Exception as e:
            print("[CLEANUP ERROR]", e)

        await asyncio.sleep(300)  # каждые 5 минут
def _load_users() -> Dict[str, dict]:
    return _load_json(USERS_FILE, {})

def _save_users(data: Dict[str, dict]):
    _save_json(USERS_FILE, data)
def _load_payments() -> Dict[str, dict]:
    data = _load_json(PAYMENTS_FILE, {})
    # Исправление: если файл payments.json оказался списком, конвертируем его в словарь
    if isinstance(data, list):
        data = {}
    return data

def _save_payments(data: Dict[str, dict]):
    _save_json(PAYMENTS_FILE, data)

# ============ CRYPTOCLOUD ============

async def cc_create_invoice(amount_usd: float, order_id: str, description: str):
    """
    Создаёт счёт в CryptoCloud. Возвращает (link, uuid) или (None, None)
    """
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

        # --- Сохраняем платёж в payments.json ---
        try:
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
        except Exception as e:
            logging.error(f"Ошибка при сохранении платежа: {e}")

        return link, uuid

    except Exception as e:
        logging.exception(f"CryptoCloud create error: {e}")
        return None, None


async def cc_is_paid(invoice_uuid: str) -> bool:
    """
    Проверяет статус счёта в CryptoCloud.
    Возвращает True, если счёт оплачен.
    """
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid):
        return False

    url = "https://api.cryptocloud.plus/v2/invoice/merchant/info"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    payload = {"uuids": [invoice_uuid]}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()
                print("[CC PAY RAW]", data)

        if data.get("status") != "success":
            logging.warning(f"⚠️ CryptoCloud вернул ошибку: {data}")
            return False

        result = data.get("result") or []
        if not result:
            logging.warning(f"⚠️ CryptoCloud вернул пустой result: {data}")
            return False

        invoice = result[0]
        status = (invoice.get("status") or "").lower()
        print(f"[CC PAY STATUS] uuid={invoice_uuid} status={status}")

        return status in ("paid", "overpaid")

    except Exception as e:
        logging.exception(f"CryptoCloud check error: {e}")
        return False
        
        # ======== TEST PAYMENT CHECK ========
from aiogram.filters import Command

@dp.message(Command("testpay"))
async def test_payment_status(m: Message):
    await m.answer("🔍 Проверяю последний платёж...")
    payments = _load_payments()
    user_id = str(m.from_user.id)
    if user_id not in payments:
        await m.answer("❌ В payments.json нет записей о платеже.")
        return
    invoice_uuid = payments[user_id].get("invoice_uuid")
    paid = await cc_is_paid(invoice_uuid)
    await m.answer(f"🧾 Статус: {'✅ Оплачен' if paid else '❌ Не найден'}\nUUID: {invoice_uuid}")
    

# ===================== FSM =====================
class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt = State()
    media = State()
    location = State()
    contact = State()
    lifetime = State()
    payment = State()
    upsell = State()
    pay_option = State()
    top_days = State()        # выбор длительности TOP
    push_confirm = State()    # подтверждение PUSH-рассылки

class AddBanner(StatesGroup):
    media = State()
    description = State()
    link = State()
    duration = State()
    payment = State()
    extend = State()


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

def kb_lifetime():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕐 1 день (бесплатно)")],
            [KeyboardButton(text="⏱ 3 дня"), KeyboardButton(text="⏱ 7 дней")],
            [KeyboardButton(text="⏱ 30 дней")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

def kb_payment():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Получить ссылку на оплату")],
            [KeyboardButton(text="✅ Я оплатил")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

def kb_upsell():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Продвижение ТОП")],
            [KeyboardButton(text="📨 Push-рассылка (30 км)")],
            [KeyboardButton(text="🖼 Баннер (премиум)")],
            [KeyboardButton(text="🌍 Разместить бесплатно (без опций)")],
            [KeyboardButton(text="← Назад")]
        ],
        resize_keyboard=True
    )


# ==================== UPSELL HANDLERS ====================

@dp.message(F.text == "⭐ Продвижение ТОП")
async def upsell_top(m: Message, state: FSMContext):
    # находим последнее событие этого пользователя
    events = _load_events()
    user_events = [e for e in events if e.get("author") == m.from_user.id]
    if not user_events:
        return await m.answer("❌ У тебя пока нет опубликованных событий для продвижения.", reply_markup=kb_main())
    current = user_events[-1]

    await state.update_data(
        opt_type="top",
        opt_event_id=current["id"],
        opt_days=None,
        _pay_uuid=None,
    )
    await m.answer("Выбери срок действия ТОП-продвижения:", reply_markup=kb_top_duration())
    await state.set_state(AddEvent.upsell)

@dp.message(F.text == "📣 Push-рассылка (30 км)")
async def upsell_push(m: Message, state: FSMContext):
    # находим последнее событие этого пользователя
    events = _load_events()
    user_events = [e for e in events if e.get("author") == m.from_user.id]
    if not user_events:
        return await m.answer("❌ У тебя пока нет опубликованных событий для PUSH.", reply_markup=kb_main())
    current = user_events[-1]

    await state.update_data(
        opt_type="push",
        opt_event_id=current["id"],
        _pay_uuid=None,
    )
    await m.answer(
        "📣 Push-рассылка уведомлений всем пользователям радиусом 30 км.\n\n"
        f"Стоимость: ${PUSH_PRICE_USD}\n\n"
        "Нажми «💳 Получить ссылку на оплату».",
        reply_markup=kb_payment()
    )
    await state.set_state(AddEvent.pay_option)


@dp.message(F.text == "🖼 Баннер (премиум)")
async def upsell_banner(m: Message, state: FSMContext):
    # используем последнее опубликованное событие как источник для баннера
    events = _load_events()
    user_events = [e for e in events if e.get("author") == m.from_user.id]
    if not user_events:
        return await m.answer("❌ У тебя пока нет опубликованных событий для баннера.", reply_markup=kb_main())
    current = user_events[-1]

    media_files = current.get("media_files") or []
    b_media = None
    if media_files:
        f = media_files[0]
        b_media = {"type": f.get("type"), "file_id": f.get("file_id")}

    parts = []
    title = current.get("title")
    if title:
        parts.append(sanitize(title))
    desc = current.get("description")
    if desc:
        parts.append(sanitize(desc))
    b_text = "\n\n".join(parts) if parts else None

    b_link = current.get("contact")

    await state.update_data(
        b_media=b_media,
        b_text=b_text,
        b_link=b_link,
        b_lat=current.get("lat"),
        b_lon=current.get("lon"),
        banner_from_upsell=True,
    )

    if current.get("lat") is None or current.get("lon") is None:
        await m.answer(
            "⚠ Для баннера желательно, чтобы у события была указана геолокация.\n"
            "Сейчас баннер будет показан без кнопки карты.",
        )

    await state.set_state(AddBanner.duration)
    await m.answer("Выберите срок показа баннера:", reply_markup=kb_banner_duration())

def kb_banner_location():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📍 Отправить мою геолокацию",
                    callback_data="bn_geo_my",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📍 Выбрать точку на карте",
                    callback_data="bn_geo_point",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➡️ Пропустить",
                    callback_data="bn_geo_skip",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="bn_geo_back",
                )
            ],
        ]
    )

def kb_banner_duration():
    rows = [
        [KeyboardButton(text="📅 1 день — $7"), KeyboardButton(text="📅 3 дня — $15")],
        [KeyboardButton(text="📅 7 дней — $30"), KeyboardButton(text="📅 15 дней — $45")],
        [KeyboardButton(text="📅 30 дней — $75")],
        [KeyboardButton(text="⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


@dp.message(AddBanner.duration)
async def banner_duration(m: Message, state: FSMContext):
    data = await state.get_data()
    if m.text in ("⬅️ Назад", "⬅ Назад", "← Назад"):
        # баннер сейчас вызывается только из апсейла события — вернёмся в апсейл
        await state.set_state(AddEvent.upsell)
        return await m.answer("Выберите дополнительную опцию:", reply_markup=kb_upsell())

    if m.text not in BANNER_DURATIONS:
        return await m.answer("Выберите один из вариантов:", reply_markup=kb_banner_duration())

    days, amount = BANNER_DURATIONS[m.text]
    await state.update_data(b_days=days, _pay_uuid=None)
    await state.set_state(AddBanner.payment)

    desc = (
        "<b>📢 Баннер</b>\n"
        "Можно разместить: картинку/видео, текст, ссылку.\n"
        "Баннер показывается всем пользователям после приветствия (в ротации до 3 шт.).\n\n"
        f"📅 Срок показа: {days} дн.\n"
        f"💵 Стоимость: ${amount}\n\n"
        "Нажмите ➜ «📎 Получить ссылку на оплату»."
    )
    await m.answer(desc, reply_markup=kb_payment())



def kb_top_duration():
    rows = [
        [KeyboardButton(text="⭐ 1 день — $1.5"), KeyboardButton(text="⭐ 3 дня — $3")],
        [KeyboardButton(text="⭐ 7 дней — $6"), KeyboardButton(text="⭐ 15 дней — $9")],
        [KeyboardButton(text="⭐ 30 дней — $15")],
        [KeyboardButton(text="← Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
    
# ======== ПЛАТНЫЕ ТАРИФЫ ========

# Тарифы баннера (цены в USD)
BANNER_PRICES = {
    "📅 1 день – $12": 12,
    "📅 3 дня – $25": 25,
    "📅 7 дней – $35": 35,
    "📅 14 дней – $70": 70,
    "📅 30 дней – $180": 180,
}

# Тарифы TOP (цены в USD)
TOP_PRICES = {
    "⭐ ТОП на 1 день – $1.5": 1.5,
    "⭐ ТОП на 3 дня – $3": 3.0,
    "⭐ ТОП на 7 дней – $6": 6.0,
    "⭐ ТОП на 15 дней – $9": 9.0,
    "⭐ ТОП на 30 дней – $15": 15.0,
}

# Стоимость push-рассылки в радиусе 30 км
PUSH_PRICE_USD = 2.0

# ===================== TEXT HELPERS =====================
def sanitize(text: str) -> str:
    return re.sub(r"[^\S\r\n]+", " ", text or "").strip()

def format_event_card(ev: dict, with_distance: Optional[float] = None) -> str:
    desc = f"\n📝 {sanitize(ev.get('description') or '')}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {sanitize(ev.get('contact') or '')}" if ev.get("contact") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    dist = f"\n📏 Расстояние: {with_distance:.1f} км" if with_distance is not None else ""
    price_part = f"\n💵 Цена: {sanitize(ev.get('price') or '')}" if ev.get("price") else ""
    dt_str = ""
    if ev.get("datetime"):
        try:
            dt = datetime.fromisoformat(ev["datetime"])
            dt_str = f"\n📅 {dt.strftime('%d.%m.%Y %H:%M')}"
        except Exception:
            dt_str = ""
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

async def send_event_media(chat_id: int, ev: dict, with_distance: Optional[float] = None):
    text = format_event_card(ev, with_distance=with_distance)
    gmap = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
    buttons = []
    if ev.get("lat") is not None and ev.get("lon") is not None:
        buttons.append([InlineKeyboardButton(text="🌐 Открыть в Google Maps", url=gmap)])
    if ev.get("id") is not None:
        buttons.append([
            InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add:{ev['id']}"),
            InlineKeyboardButton(text="🔗 Поделиться", callback_data=f"share_ev:{ev['id']}")
        ])
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
            await bot.send_message(chat_id, " ", reply_markup=ikb)  # отдельной кнопкой
    elif len(media) == 1:
        f = media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=ikb)
        elif f["type"] == "video":
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=ikb)
    else:
        await bot.send_message(chat_id, text, reply_markup=ikb)


async def send_push_for_event(ev: dict) -> int:
    """Рассылает событие всем пользователям в радиусе DEFAULT_RADIUS_KM от локации события.
    Возвращает количество получателей."""
    try:
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
    except Exception as e:
        logging.exception(f"Ошибка в send_push_for_event: {e}")
        return 0

async def send_banner(chat_id: int, b: dict):
    cap = format_banner_caption(b)
    if b.get("media_type") == "photo" and b.get("file_id"):
        await bot.send_photo(chat_id, b["file_id"], caption=cap, parse_mode="HTML")
    elif b.get("media_type") == "video" and b.get("file_id"):
        await bot.send_video(chat_id, b["file_id"], caption=cap, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, cap, parse_mode="HTML")

# ===================== START / WELCOME =====================
async def send_logo_then_welcome(m: Message):
    # показать лого, если есть
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

    await asyncio.sleep(1.0)

    welcome_text = (
        "👋 Добро пожаловать в <b>PartyRadar</b>!\n"
        "📍 Сервис по поиску людей и событий по геолокации. Всё завязано на точку на карте.\n\n"
        "🎉 <b>Для пользователей:</b>\n"
        "• Вечеринки, встречи по интересам, свидания, спорт и любое другое событие.\n"
        "• 🛒 Маркет — объявления «Куплю/Продам» рядом.\n"
        "• 💼 Работа — вакансии и те, кто ищет работу рядом.\n"
        "• ✨ Покажи себя — самопрезентация, соцсети, знакомства рядом.\n"
        "• 🔍 Ищу тебя — поиск людей и пропавших питомцев.\n"
        "• 🎊 Поздравления — хорошие новости и тёплые слова.\n\n"
        "📅 Базовые объявления живут 24 часа бесплатно (можно продлить).\n"
        "💎 Для продвижения есть платные сроки, ТОП для событий и рекламные баннеры.\n\n"
        "📈 <b>Для бизнеса и предпринимателей:</b>\n"
        "• Локальная реклама по геолокации — показы баннеров людям рядом.\n"
        "• Аналитика по интересу: какие объявления собирают больше переходов и откликов.\n"
        "• Продвижение событий и офферов через ТОП и баннеры.\n\n"
        "👇 Выбирай действие из меню:"
    )

    # мгновенное отображение приветствия (без печати по буквам)
    await m.answer(welcome_text, parse_mode="HTML")

    # показать до 3 активных баннеров
    banners = _load_banners()
    now = datetime.now()
    actives = [b for b in banners if _safe_dt(b.get("expire")) and _safe_dt(b.get("expire")) > now]
    actives.sort(key=lambda x: x.get("id", 0), reverse=True)
    for b in actives[:MAX_ACTIVE_BANNERS]:
        try:
            await send_banner(m.chat.id, b)
        except Exception:
            pass

def _safe_dt(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None

@dp.message(Command("start"))
async def start_cmd(m: Message, state: FSMContext):

    # Полный сброс FSM — решает зависание старых шагов
    await state.clear()

    await send_logo_then_welcome(m)
    await m.answer("Главное меню:", reply_markup=kb_main())

# ===================== SUPPORT =====================
@dp.message(F.text == "📩 Связаться с нами")
async def support(m: Message):
    await m.answer(
        "💬 Если у тебя есть идеи, жалобы или предложения — пиши напрямую администратору проекта:\n"
        "👉 <b>@drscorohod</b>\n\n"
        "Мы читаем все сообщения и стараемся улучшать сервис ❤️",
        reply_markup=kb_main()
    )

# ===================== СОБЫТИЯ: СОЗДАНИЕ =====================
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
        return await m.answer("📝 Введи название:", reply_markup=kb_back())
    await state.update_data(description=sanitize(m.text))
    await state.set_state(AddEvent.category)
    await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())

@dp.message(AddEvent.category)
async def ev_cat(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer("🧾 Введи описание:", reply_markup=kb_back())
    cat = sanitize(m.text)
    await state.update_data(category=cat)

    # Маркет: куплю/продам — вместо даты спрашиваем цену
    if cat in ("🛒 Куплю", "💰 Продам"):
        await state.set_state(AddEvent.dt)
        return await m.answer(
            "💵 Укажи цену (можно с символом валюты):\n"
            "Например: <b>150€</b>, <b>200$</b>, <b>5000₽</b> или <b>договорная</b>.",
            reply_markup=kb_back()
        )

    # Работа — без даты, сразу к медиа
    if cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
        await state.update_data(datetime=None, price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "💼 Это рубрика для объявлений о работе.\n"
            "Опиши, кого ты ищешь или какую работу сам ищешь — и прикрепи медиа, если нужно.\n\n"
            "📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n"
            "📍 Скрепка → Геопозиция → точка на карте.\n"
            "⚠ Аудио и кружки не поддерживаются.",
            reply_markup=kb_media_step()
        )

    # Покажи себя — без даты, объяснение рубрики
    if cat == "✨ Покажи себя":
        await state.update_data(datetime=None, price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "✨ <b>Покажи себя</b> — рубрика для самопрезентации и знакомств.\n"
            "Здесь можно рассказать о себе, показать свой стиль, дать ссылки на соцсети и найти людей рядом.\n\n"
            "📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n"
            "📍 Скрепка → Геопозиция → точка на карте.\n"
            "⚠ Аудио и кружки не поддерживаются.",
            reply_markup=kb_media_step()
        )

    # Ищу тебя — без даты
    if cat == "🔍 Ищу тебя":
        await state.update_data(datetime=None, price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "🔍 <b>Ищу тебя</b> — рубрика для поиска людей, с которыми ты потерял контакт, и пропавших животных.\n"
            "Опиши, кого ты ищешь, где примерно видел, и прикрепи фото, если оно есть.\n\n"
            "📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n"
            "📍 Скрепка → Геопозиция → точка на карте.\n"
            "⚠ Аудио и кружки не поддерживаются.",
            reply_markup=kb_media_step()
        )

    # Поздравления — тоже без даты
    if cat == "🎊 Поздравления":
        await state.update_data(datetime=None, price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "🎊 <b>Поздравления</b> — рубрика для тёплых слов, пожеланий и хороших новостей.\n"
            "Поздравь друга, родных или всех вокруг — прикрепи фото/видео и поделись моментом 🙌\n\n"
            "📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n"
            "📍 Скрепка → Геопозиция → точка на карте.\n"
            "⚠ Аудио и кружки не поддерживаются.",
            reply_markup=kb_media_step()
        )

    # Обычные события — спрашиваем дату/время
    await state.set_state(AddEvent.dt)
    await m.answer(
        "📆 Введи дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n"
        "Пример: 25.10.2025 19:30",
        reply_markup=kb_back()
    )


@dp.message(AddEvent.dt)
async def ev_dt(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category)
        return await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())
    data = await state.get_data()
    cat = data.get("category")

    # Маркет: цена вместо даты
    if cat in ("🛒 Куплю", "💰 Продам"):
        await state.update_data(price=sanitize(m.text), datetime=None, media_files=[])
    else:
        try:
            dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
            if dt <= datetime.now():
                return await m.answer("⚠ Нельзя указывать прошедшее время.", reply_markup=kb_back())
        except ValueError:
            return await m.answer("⚠ Неверный формат. Пример: 25.10.2025 19:30", reply_markup=kb_back())
        await state.update_data(datetime=dt.isoformat(), media_files=[])
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
    await m.answer(f"✅ Файл добавлен ({len(files)}/3). "
                   + ("Можно добавить ещё или " if left else "")
                   + "отправь геолокацию для следующего шага.", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE}))
async def ev_media_unsupported(m: Message, state: FSMContext):
    await m.answer("⚠ Аудио и кружки не поддерживаются. Прикрепи фото/видео.", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.text == "⬅ Назад")
async def ev_media_back(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    if files:
        files.pop()
        await state.update_data(media_files=files)
        return await m.answer(f"🗑 Удалён последний файл ({len(files)}/3).", reply_markup=kb_media_step())
    await state.set_state(AddEvent.dt)
    await m.answer("📆 Вернулись к дате/времени. Введи ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=kb_back())

@dp.message(AddEvent.media, F.location)
async def ev_media_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    # сохраним активность пользователя (для будущих пушей по событиям)
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    u["last_location"] = {"lat": m.location.latitude, "lon": m.location.longitude}
    u["last_seen"] = datetime.now().isoformat()
    users[str(m.from_user.id)] = u
    _save_users(users)
    await state.set_state(AddEvent.contact)
    await m.answer("☎ Укажи контакт (@username, телефон или ссылка). Или напиши «Пропустить».", reply_markup=kb_back())


@dp.message(AddEvent.contact)
async def ev_contact(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer("Вернулись к медиафайлам:", reply_markup=kb_media_step())
    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=sanitize(m.text))
    await state.set_state(AddEvent.lifetime)
    await m.answer("⏳ Выбери срок жизни объявления:", reply_markup=kb_lifetime())

@dp.message(AddEvent.lifetime)
async def ev_lifetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.contact)
        return await m.answer("☎ Укажи контакт или напиши «Пропустить».", reply_markup=kb_back())
    if m.text not in LIFETIME_OPTIONS:
        return await m.answer("Выбери вариант из списка:", reply_markup=kb_lifetime())
    hours = LIFETIME_OPTIONS[m.text]

    data = await state.get_data()
    # Автоматическая модерация — перед любым размещением/оплатой
    ok, reason = check_event_moderation(data)
    if not ok:
        await state.clear()
        return await m.answer(
            reason + "\n\nПопробуй переписать текст более нейтрально 🙏",
            reply_markup=kb_main()
        )

    if hours == 24:
        await publish_event(m, data, hours)
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "💡 Доп. опции:\n"
            "⭐ <b>ТОП на 7 дней</b> — ваше событие будет показываться первым в выдаче региона.\n\n"
            "Выберите опцию или разместите бесплатно.",
            reply_markup=kb_upsell()
        )

    amount = TARIFFS_USD[hours]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None)
    desc = (
        f"⏳ <b>Платный срок показа</b>\nВы выбрали: <b>{m.text}</b>\nСтоимость: <b>${amount}</b>\n\n"
        "Нажмите «💳 Получить ссылку на оплату». Счёт в USD, оплата TON/USDT."
    )
    await state.set_state(AddEvent.payment)
    await m.answer(desc
, reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def ev_pay_get(m: Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get("paid_lifetime")
    if not hours:
        return await m.answer("❌ Нет активного платного тарифа.", reply_markup=kb_payment())
    amount = TARIFFS_USD[hours]
    order_id = f"lifetime_{hours}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    order_id = str(m.from_user.id)
    link, invoice_id = await cc_create_invoice(amount, order_id, f"PartyRadar: event lifetime {hours}h")

    if not link:
        return await m.answer("⚠️ Не удалось получить ссылку на счёт. Проверь API ключ.", reply_markup=kb_payment())

# сохраняем платёж по user_id, чтобы потом можно было найти при проверке
    pay = _load_payments()
    pay[str(m.from_user.id)] = {
        "type": "event_lifetime",
        "user_id": m.from_user.id,
        "invoice_uuid": invoice_id,
        "payload": {"hours": hours, "data": data}
    }
    _save_payments(pay)

    # ВАЖНО: сохраняем uuid в FSM сейчас
    await state.update_data(_pay_uuid=invoice_id)

    await m.answer(
        f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите ✅ Я оплатил.\n\n⚠️ Платёжная система может взять дополнительную комиссию.",
        reply_markup=kb_payment()
    )


@dp.message(AddEvent.payment, F.text == "✅ Я оплатил")
async def ev_pay_check(m: Message, state: FSMContext):
    data = await state.get_data()
    invoice_uuid = data.get("_pay_uuid")
    hours = data.get("paid_lifetime")

    if not invoice_uuid:
        await m.answer(
            "⚠️ Ошибка: не найден идентификатор счёта.\n"
            "Попробуй заново получить ссылку на оплату.",
            reply_markup=kb_payment()
        )
        return

    await m.answer("🔍 Проверяю оплату...")
    paid = await cc_is_paid(invoice_uuid)

    if not paid:
        return await m.answer(
            "❌ Оплата пока не найдена.\n\n"
            "Если ты только что оплатил — подожди 1–2 минуты и нажми «✅ Я оплатил» ещё раз.\n"
            "Если не оплачивал — перейди по ссылке и заверши оплату.",
            reply_markup=kb_payment()
        )

    await m.answer("☑️ Оплата подтверждена! Публикую событие...")
    await publish_event(m, data, hours)
    await state.clear()

    await m.answer("✅ Событие опубликовано!", reply_markup=kb_main())

    events = _load_events()
    if events:
        ev = events[-1]
        try:
            await send_event_media(m.chat.id, ev)
        except Exception:
            pass
@dp.message(AddEvent.payment, F.text == "← Назад")
async def ev_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    await m.answer("🔙 Вернулись к выбору срока:", reply_markup=kb_lifetime())

@dp.message(AddEvent.upsell)
async def ev_upsell(m: Message, state: FSMContext):
    txt = m.text

    if txt in ("⬅️ Назад", "⬅ Назад", "← Назад"):
        # предыдущий шаг после апсейла — просто показать главное меню
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    # 🔥 Новый пункт меню — выбор сроков ТОП-продвижения
    if txt == "🌍 Разместить бесплатно (без опций)":
        await state.clear()
        return await m.answer("✔️ Готово! Событие опубликовано.", reply_markup=kb_main())

    # Ловим выбор срока ТОП-продвижения
    if txt.startswith("⭐ "):
        try:
            days = int(txt.split()[1])
        except Exception:
            return await m.answer("❌ Ошибка: не удалось определить срок.", reply_markup=kb_top_duration())

        if days not in TOP_PRICES:
            return await m.answer("❌ Ошибка: такого срока нет.", reply_markup=kb_top_duration())

        price = TOP_PRICES[days]

        # находим последнее событие этого пользователя
        events = _load_events()
        user_events = [e for e in events if e.get("author") == m.from_user.id]
        if not user_events:
            return await m.answer("❌ У тебя пока нет опубликованных событий для ТОП.", reply_markup=kb_main())
        current = user_events[-1]

        await state.set_state(AddEvent.pay_option)
        await state.update_data(
            opt_type="top",
            opt_event_id=current["id"],
            opt_days=days,
            _pay_uuid=None,
        )

        return await m.answer(
            f"⭐ ТОП-продвижение на {days} дней. Стоимость: ${price}.",
            reply_markup=kb_payment(),
        )

    return await m.answer("Выберите опцию из меню.", reply_markup=kb_upsell())

@dp.message(StateFilter(AddEvent.pay_option), F.text == "💳 Получить ссылку на оплату")
async def ev_opt_link(m: Message, state: FSMContext):
    data = await state.get_data()
    opt_type = data.get("opt_type")
    ev_id = data.get("opt_event_id")
    days = data.get("opt_days")

    if opt_type not in ("top", "push") or not ev_id:
        return await m.answer("❌ Не выбрана платная опция.", reply_markup=kb_upsell())

    if opt_type == "top":
        if not days:
            return await m.answer("Сначала выбери срок ТОП-продвижения.", reply_markup=kb_top_duration())
        amount = TOP_PRICES.get(days)
        if not amount:
            return await m.answer("❌ Ошибка: неверный срок ТОП.", reply_markup=kb_top_duration())
        desc = f"PartyRadar: ТОП на {days} дн. для события #{ev_id}"
    else:
        amount = PUSH_PRICE_USD
        desc = f"PartyRadar: PUSH для события #{ev_id}"

    order_id = str(m.from_user.id)
    link, invoice_id = await cc_create_invoice(amount, order_id, desc)
    if not link or not invoice_id:
        return await m.answer("⚠️ Не удалось создать счёт. Попробуй позже.", reply_markup=kb_payment())

    # Сохраняем платёж по user_id (как и для продления/срока события)
    pay = _load_payments()
    pay[str(m.from_user.id)] = {
        "type": opt_type,
        "user_id": m.from_user.id,
        "invoice_uuid": invoice_id,
        "payload": {
            "event_id": ev_id,
            "days": days if opt_type == "top" else None,
        },
    }
    _save_payments(pay)

    await state.update_data(_pay_uuid=invoice_id)

    await m.answer(
        f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажми \"💳 Я оплатил\".\n\n⚠️ Платёжная система может взять дополнительную комиссию.",
        reply_markup=kb_payment(),
    )

from aiogram.filters import StateFilter

@dp.message(StateFilter(AddEvent.pay_option), F.text == "💳 Я оплатил")
async def ev_opt_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    opt_type = data.get("opt_type")
    ev_id = data.get("opt_event_id")
    days = data.get("opt_days")

    if not uuid:
        return await m.answer("❌ Счёт не найден.", reply_markup=kb_payment())

    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    events = _load_events()
    target = next((e for e in events if e["id"] == ev_id), None)
    if not target:
        await state.clear()
        return await m.answer("❌ Событие не найдено.", reply_markup=kb_main())

    if opt_type == "top":
        if not days:
            return await m.answer("❌ Ошибка: не задан срок ТОП.", reply_markup=kb_top_duration())
        target["is_top"] = True
        target["top_expire"] = (datetime.now() + timedelta(days=days)).isoformat()
        _save_events(events)
        await state.clear()
        return await m.answer(
            f"🎉 ТОП активирован на *{days} дней*!",
            reply_markup=kb_main(),
            parse_mode="Markdown",
        )

    if opt_type == "push":
        sent = await send_push_for_event(target)
        await state.clear()
        return await m.answer(
            f"📣 PUSH-рассылка отправлена. Получателей: {sent}.",
            reply_markup=kb_main(),
        )

    await state.clear()
    return await m.answer("✅ Оплата обработана.", reply_markup=kb_main())

@dp.message(AddEvent.pay_option, F.text == "⬅ Назад")
async def ev_opt_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer("Выберите дополнительную опцию:", reply_markup=kb_upsell())
async def publish_event(m: Message, data: dict, hours: int):
    media_files = data.get("media_files", [])
    if not media_files:
        # подставим лого как заглушку
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
        "is_top": False,
        "top_expire": None
    }
    _save_events(events + [ev])


# ======== АВТОМАТИЧЕСКАЯ МОДЕРАЦИЯ СОДЕРЖИМОГО =========

FORBIDDEN_KEYWORDS_GROUPS = {
    "adult": [
        "интим", "эскорт", "секс услуги", "sex услуги", "минет", "очень откровенные фото",
        "onlyfans", "онлифанс", "порн", "pornhub", "эротик"
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


def _check_text_moderation(text: str) -> tuple[bool, str | None]:
    t = _normalize_text(text)

    # Ссылки на запрещённые домены
    for dom in FORBIDDEN_DOMAINS:
        if dom in t:
            return False, "Объявление содержит запрещённые ссылки или ресурсы."

    # Подозрительные короткие ссылки
    for dom in SUSPICIOUS_SHORTLINKS:
        if dom in t:
            return False, "Объявление содержит подозрительные сокращённые ссылки."

    # Ключевые группы
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


def check_event_moderation(data: dict) -> tuple[bool, str | None]:
    parts = []
    title = data.get("title")
    desc = data.get("description")
    contact = data.get("contact")
    category = data.get("category")

    if title:
        parts.append(str(title))
    if desc:
        parts.append(str(desc))
    if contact:
        parts.append(str(contact))
    if category:
        parts.append(str(category))

    full_text = "\n".join(parts)
    return _check_text_moderation(full_text)


# ===================== ПОИСК СОБЫТИЙ =====================
@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message, state: FSMContext):
    await state.set_state(SearchEvents.menu)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Все события рядом")],
            [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="💼 Работа")],
            [KeyboardButton(text="✨ Покажи себя"), KeyboardButton(text="🔍 Ищу тебя")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )
    await m.answer(
        "Что ищем? Выбери раздел:\n"
        "🔎 Все события — обычные встречи, вечеринки, спорт и т.п.\n"
        "🛒 Маркет — объявления «Куплю/Продам» рядом.\n"
        "💼 Работа — вакансии и те, кто ищет работу.\n"
        "✨ Покажи себя — анкеты и самопрезентации.\n"
        "🔍 Ищу тебя — поиск людей и пропавших питомцев.",
        reply_markup=kb
    )


@dp.message(SearchEvents.menu)
async def search_menu_router(m: Message, state: FSMContext):
    text = m.text or ""
    if text == "⬅ Назад":
        await state.clear()
        return await m.answer("Возвращаю в главное меню ☺️", reply_markup=kb_main())

    if text == "🔎 Все события рядом":
        await state.set_state(SearchEvents.all)
    elif text == "🛒 Маркет":
        await state.set_state(SearchEvents.market)
    elif text == "💼 Работа":
        await state.set_state(SearchEvents.work)
    elif text == "✨ Покажи себя":
        await state.set_state(SearchEvents.selfpromo)
    elif text == "🔍 Ищу тебя":
        await state.set_state(SearchEvents.findyou)
    else:
        return await m.answer("Пожалуйста, выбери вариант из списка ☺️", reply_markup=kb_main())

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )
    await m.answer(
        "📍 Отправь геолокацию (скрепка → Геопозиция → точка на карте),\n"
        f"я покажу подходящие объявления в радиусе ~ {DEFAULT_RADIUS_KM} км.",
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
    found.sort(key=lambda x: ((0 if x[0].get("is_top") else 1), x[1]))

    await state.clear()

    if not found:
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="➕ Создать событие")],
                [KeyboardButton(text="⬅ Назад")]
            ],
            resize_keyboard=True
        )
        return await m.answer(
            "😔 Ничего рядом не найдено.\nМожешь создать своё объявление!",
            reply_markup=kb
        )

    for ev, dist in found:
        try:
            await send_event_media(m.chat.id, {**ev}, with_distance=dist)
        except Exception:
            txt = format_event_card(ev, with_distance=dist)
            gmap = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
            ikb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Открыть в Google Maps", url=gmap)]
            ])
            await m.answer(txt, reply_markup=ikb)


@dp.message(SearchEvents.all, F.location)
async def search_all_with_location(m: Message, state: FSMContext):
    user_loc = (m.location.latitude, m.location.longitude)
    await _search_and_show(m, user_loc, "all", state)


@dp.message(SearchEvents.market, F.location)
async def search_market_with_location(m: Message, state: FSMContext):
    user_loc = (m.location.latitude, m.location.longitude)
    await _search_and_show(m, user_loc, "market", state)


@dp.message(SearchEvents.work, F.location)
async def search_work_with_location(m: Message, state: FSMContext):
    user_loc = (m.location.latitude, m.location.longitude)
    await _search_and_show(m, user_loc, "work", state)


@dp.message(SearchEvents.selfpromo, F.location)
async def search_selfpromo_with_location(m: Message, state: FSMContext):
    user_loc = (m.location.latitude, m.location.longitude)
    await _search_and_show(m, user_loc, "selfpromo", state)


@dp.message(SearchEvents.findyou, F.location)
async def search_findyou_with_location(m: Message, state: FSMContext):
    user_loc = (m.location.latitude, m.location.longitude)
    await _search_and_show(m, user_loc, "findyou", state)


@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_pay_link(m: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("b_days")
    if not days:
        return await m.answer("❌ Срок не выбран.", reply_markup=kb_banner_duration())

    # Проверяем, нет ли уже активного баннера в радиусе DEFAULT_RADIUS_KM от этой точки
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
                    "❌ В этом районе уже есть активный баннер.\n\n"
                    "Баннер можно разместить, когда текущий баннер истечёт.",
                    reply_markup=kb_main()
                )

    amount = None
    for k, (d, a) in BANNER_DURATIONS.items():
        if d == days:
            amount = a
            break
    if amount is None:
        return await m.answer("❌ Тариф не найден.", reply_markup=kb_banner_duration())

    order_id = f"banner_{m.from_user.id}_{int(datetime.now().timestamp())}_{days}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи.", reply_markup=kb_payment())

    # save pending
    pay = _load_payments()
    pay[uuid] = {"type": "banner_buy", "user_id": m.from_user.id, "payload": data}
    _save_payments(pay)

    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил»\n\n⚠️ Платёжная система может взять дополнительную комиссию.", reply_markup=kb_payment())

@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("❌ Счёт не найден. Получите ссылку ещё раз.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    # публикуем баннер
    d = await state.get_data()
    media = d.get("b_media")
    if not media:
        return await m.answer("❌ Медиа не найдено. Начните заново.", reply_markup=kb_main())

    text = d.get("b_text")
    link = d.get("b_link")
    lat = d.get("b_lat")
    lon = d.get("b_lon")
    days = d.get("b_days", 1)

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
    await m.answer("✅ Баннер активирован и будет показан пользователям после приветствия.", reply_markup=kb_main())

@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("⏳ Выберите срок показа баннера:", reply_markup=kb_banner_duration())

# ===================== ПУШ-ДЕЙМОНЫ =====================
async def push_daemon():
    """Пуш за 2 часа до окончания событий и баннеров, снятие истёкшего ТОПа."""
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
                    # показываем кнопки продления (через меню создания события — как решили ранее)
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📅 +1 день", callback_data=f"extend_ev:{ev['id']}:24")],
                        [InlineKeyboardButton(text="⏱ +3 дня", callback_data=f"extend_ev:{ev['id']}:72")],
                        [InlineKeyboardButton(text="⏱ +7 дней", callback_data=f"extend_ev:{ev['id']}:168")],
                        [InlineKeyboardButton(text="⏱ +30 дней", callback_data=f"extend_ev:{ev['id']}:720")]
                    ])
                    try:
                        await bot.send_message(ev["author"], f"⏳ Событие «{ev['title']}» скоро завершится. Хотите продлить?", reply_markup=kb)
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
                    # кнопки продления баннера — те же сроки
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📆 +1 день", callback_data=f"extend_bn:{b['id']}:1")],
                        [InlineKeyboardButton(text="📆 +3 дня", callback_data=f"extend_bn:{b['id']}:3")],
                        [InlineKeyboardButton(text="📆 +7 дней", callback_data=f"extend_bn:{b['id']}:7")],
                        [InlineKeyboardButton(text="📆 +14 дней", callback_data=f"extend_bn:{b['id']}:14")],
                        [InlineKeyboardButton(text="📆 +30 дней", callback_data=f"extend_bn:{b['id']}:30")]
                    ])
                    try:
                        await bot.send_message(b["owner"], "⏳ Срок показа баннера заканчивается. Продлить?", reply_markup=kb)
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
        ev_id = int(ev_id); hours = int(hours)
        amount = TARIFFS_USD.get(hours)
        if not amount:
            return await cq.answer("Тариф не найден", show_alert=True)
        order_id = f"extend_event_{ev_id}_{cq.from_user.id}_{hours}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar event extend {hours}h")
        if not link:
            return await cq.answer("Не удалось создать счёт", show_alert=True)
        # save pending
        pay = _load_payments()
        pay[uuid] = {"type": "event_extend", "user_id": cq.from_user.id, "payload": {"event_id": ev_id, "hours": hours}}
        _save_payments(pay)
        await cq.message.answer(f"💳 Ссылка на оплату продления:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил»\n\n⚠️ Платёжная система может взять дополнительную комиссию.")
        await cq.answer()
    except Exception:
        await cq.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("extend_bn:"))
async def cb_extend_banner(cq: CallbackQuery):
    try:
        _, b_id, days = cq.data.split(":")
        b_id = int(b_id); days = int(days)
        # найти цену
        amount = None
        for _, (d, a) in BANNER_DURATIONS.items():
            if d == days:
                amount = a
                break
        if amount is None:
            return await cq.answer("Тариф не найден", show_alert=True)
        order_id = f"extend_banner_{b_id}_{cq.from_user.id}_{days}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner extend {days}d")
        if not link:
            return await cq.answer("Не удалось создать счёт", show_alert=True)
        pay = _load_payments()
        pay[uuid] = {"type": "banner_extend", "user_id": cq.from_user.id, "payload": {"banner_id": b_id, "days": days}}
        _save_payments(pay)
        await cq.message.answer(f"💳 Ссылка на оплату продления баннера:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил»\n\n⚠️ Платёжная система может взять дополнительную комиссию.")
        await cq.answer()
    except Exception:
        await cq.answer("Ошибка", show_alert=True)

# ===================== ВЕБХУК ДЛЯ CRYPTOCLOUD =====================
# Ожидается, что Render отдаёт порт в PORT
async def handle_payment_callback(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = await request.text()
        logging.info(f"callback non-json: {body}")
        return web.Response(text="ok")

    # ожидаемые поля CC: result: {uuid, status, order_id, ...}
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

    t = entry.get("type")
    payload = entry.get("payload", {})
    user_id = entry.get("user_id")
    try:
        if t == "event_lifetime":
            hours = payload.get("hours")
            data = payload.get("data")
            # публикуем событие
            class Dummy:  # для совместимости с publish_event
                def __init__(self, uid): self.from_user = type("U", (), {"id": uid})
            await publish_event(Dummy(user_id), data, int(hours))
            await bot.send_message(user_id, "✅ Оплата подтверждена. Событие опубликовано!")
        elif t == "event_extend":
            ev_id = payload["event_id"]; hours = int(payload["hours"])
            events = _load_events()
            target = next((e for e in events if e["id"] == ev_id), None)
            if target:
                exp = _safe_dt(target.get("expire")) or datetime.now()
                target["expire"] = (max(exp, datetime.now()) + timedelta(hours=hours)).isoformat()
                target["notified"] = False
                _save_events(events)
                await bot.send_message(user_id, "✅ Событие продлено!")
        elif t == "event_top":
            ev_id = payload["event_id"]
            events = _load_events()
            target = next((e for e in events if e["id"] == ev_id), None)
            if target:
                target["is_top"] = True
                target["top_expire"] = (datetime.now() + timedelta(days=7)).isoformat()
                _save_events(events)
                await bot.send_message(user_id, "✅ ТОП активирован на 7 дней!")
        elif t == "banner_buy":
            d = payload
            media = d.get("b_media")
            if media:
                text = d.get("b_text")
                link = d.get("b_link")
                lat = d.get("b_lat")
                lon = d.get("b_lon")
                days = d.get("b_days", 1)

                banners = _load_banners()
                new_id = (banners[-1]["id"] + 1) if banners else 1
                expire = datetime.now() + timedelta(days=days)
                banners.append({
                    "id": new_id,
                    "owner": user_id,
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
                await bot.send_message(user_id, "✅ Баннер активирован и будет показан пользователям.")
        elif t == "banner_extend":
            b_id = payload["banner_id"]; days = int(payload["days"])
            banners = _load_banners()
            b = next((x for x in banners if x["id"] == b_id), None)
            if b:
                exp = _safe_dt(b.get("expire")) or datetime.now()
                b["expire"] = (max(exp, datetime.now()) + timedelta(days=days)).isoformat()
                b["notified"] = False
                _save_banners(banners)
                await bot.send_message(user_id, "✅ Баннер продлён!")
        # удаляем запись о платеже
        pay.pop(uuid, None)
        _save_payments(pay)
    except Exception as e:
        logging.exception(f"callback process error: {e}")

    return web.Response(text="ok")

# ===================== АВТО-ОЧИСТКА =====================
async def cleanup_daemon():
    """Удаление истёкших событий/баннеров + уведомление об удалении."""
    while True:
        try:
            now = datetime.now()
            # events
            events = _load_events()
            updated = []
            for ev in events:
                exp = _safe_dt(ev.get("expire"))
                if exp and exp <= now:
                    try:
                        await bot.send_message(ev["author"], f"🗑 Событие «{ev['title']}» истекло и удалено.")
                    except Exception:
                        pass
                else:
                    updated.append(ev)
            if len(updated) != len(events):
                _save_events(updated)

            # banners
            banners = _load_banners()
            banners_updated = []
            for b in banners:
                exp = _safe_dt(b.get("expire"))
                if exp and exp <= now:
                    # просто удаляем без уведомления или можно уведомить владельца
                    try:
                        await bot.send_message(b["owner"], "🗑 Срок баннера истёк. Он удалён из ротации.")
                    except Exception:
                        pass
                else:
                    banners_updated.append(b)
            if len(banners_updated) != len(banners):
                _save_banners(banners_updated)

        except Exception as e:
            logging.exception(f"cleanup_daemon error: {e}")
        await asyncio.sleep(600)

# ===================== ГЛОБАЛЬНЫЕ ОБРАБОТЧИКИ =====================
@dp.message(F.text == "⬅ Назад")
async def banner_back_router(m: Message, state: FSMContext):

    st = await state.get_state()

    # === Создание баннера ===

    # 1) Назад из описания → в загрузку медиа
    if st == AddBanner.description:
        await state.set_state(AddBanner.media)
        return await m.answer("📸 Загрузите фото/видео баннера:", reply_markup=kb_back())

    # 2) Назад из ввода ссылки → в описание
    if st == AddBanner.link:
        await state.set_state(AddBanner.description)
        return await m.answer("✏️ Добавьте описание баннера:", reply_markup=kb_skip_back())

    # 3) Назад из выбора способа локации → в шаг со ссылкой
    if st == "await_banner_geo":
        await state.set_state(AddBanner.link)
        return await m.answer(
            "🔗 Укажите ссылку (или «Пропустить»).",
            reply_markup=kb_skip_back()
        )

    # 4) Назад из «Отправить геолокацию» или «Выбрать на карте» → в меню выбора типа локации
    if st == "await_banner_geo_my" or st == "await_banner_geo_point":
        await state.set_state("await_banner_geo")
        return await m.answer(
            "📍 Укажите локацию баннера (необязательно):\n"
            "- можно отправить свою геолокацию,\n"
            "- выбрать точку на карте,\n"
            "- или пропустить этот шаг.",
            reply_markup=kb_banner_location()
        )

    # 5) Назад из выбора длительности → вернуться к выбору локации
    if st == AddBanner.duration:
        await state.set_state("await_banner_geo")
        return await m.answer(
            "📍 Укажите локацию баннера (необязательно):",
            reply_markup=kb_banner_location()
        )

    # 6) Назад из оплаты → в выбор длительности
    if st == AddBanner.payment:
        await state.set_state(AddBanner.duration)
        return await m.answer(
            "⏳ Выберите срок показа баннера:",
            reply_markup=kb_banner_duration()
        )

    # === Если состояние неизвестно → отправляем в главное меню ===
    await state.clear()
    return await m.answer("Главное меню:", reply_markup=kb_main())
    

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


@dp.callback_query(F.data.startswith("share_ev:"))
async def cb_share_ev(cq: CallbackQuery):
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
        return await cq.answer("Событие уже недоступно 🕒", show_alert=True)

    await cq.message.answer(
        "Вот копия события 👇\n"
        "Её можно переслать в чаты, сохранить медиа и выложить в другие соцсети 🙌"
    )
    await send_event_media(cq.from_user.id, ev)
    await cq.answer()


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


@dp.message()
async def fallback(m: Message):
    if not m.text:
        return
    await m.answer("Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())

# ================= RUN APP (Render webhook only) =================

from aiogram.webhook.aiohttp_server import SimpleRequestHandler

async def make_web_app():
    app = web.Application()
    app.router.add_post("/payment_callback", handle_payment_callback)
    app.router.add_get("/payment_callback", handle_payment_callback)

    # создаём webhook handler из aiogram
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    return app


async def on_startup():
    webhook_url = f"{os.getenv('PUBLIC_URL')}/webhook"
    await bot.set_webhook(webhook_url)
    logging.info(f"🚀 Webhook set to {webhook_url}")


async def main():
    app = await make_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()

    await on_startup()
    logging.info("✅ Webhook server running on port 10000")

    # фоновые задачи
    asyncio.create_task(push_daemon())
    asyncio.create_task(cleanup_daemon())

    # чтобы процесс не завершался
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("🛑 Server stopped manually")
