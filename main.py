# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
from geopy.distance import geodesic
from aiogram import Bot, Dispatcher, F
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
    ContentType
)
from dotenv import load_dotenv

# ==================== CONFIG ====================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip() or os.getenv("CRYPTOCLOUD_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())

EVENTS_FILE = "events.json"
BANNERS_FILE = "banners.json"
USERS_FILE = "users.json"

DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2

# баннерные слоты по региону
MAX_BANNERS_PER_REGION = 3
BANNER_REGION_RADIUS_KM = 30  # км

# цены (USD)
PRICES = {
    "extend_48h": 2.0,
    "extend_week": 5.0,
    "extend_2week": 8.0,
    "top_week": 10.0,
    "push": 5.0,
    "banner_week": 10.0,
    "banner_month": 30.0,
}

# выбор жизни объявления
LIFETIME_OPTIONS_MAP = {
    "24": {"price": 0.0, "label": "🕐 24 часа (бесплатно)", "hours": 24},
    "48": {"price": PRICES["extend_48h"], "label": "📅 48 часов", "hours": 48},
    "168": {"price": PRICES["extend_week"], "label": "🗓 1 неделя", "hours": 168},
    "336": {"price": PRICES["extend_2week"], "label": "🏷 2 недели", "hours": 336},
}
TARIFFS_USD = {v["hours"]: v["price"] for v in LIFETIME_OPTIONS_MAP.values() if v["hours"] != 24}

# ==================== STORAGE ====================
def _ensure_file(path: str, default_content: str):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(default_content)

def _load_json(path: str, default):
    _ensure_file(path, "{}" if isinstance(default, dict) else ("[]" if isinstance(default, list) else ""))
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

# ==================== CRYPTOCLOUD ====================
async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Создаёт счёт в CryptoCloud. Возвращает (link, uuid) или (None, None) при ошибке.
    Поддерживает как v2 (с shop_id), так и v1 (без).
    """
    if not CRYPTOCLOUD_API_KEY:
        return None, None

    # v2 API предпочтительнее
    if CRYPTOCLOUD_SHOP_ID:
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
                    link = data.get("result", {}).get("link") or data.get("pay_url")
                    uuid = data.get("result", {}).get("uuid") or data.get("uuid")
                    return link, uuid
        except Exception as e:
            logging.exception(f"CryptoCloud v2 create error: {e}")

    # fallback v1
    url = "https://api.cryptocloud.plus/v1/invoice/create"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    payload = {
        "amount": f"{amount_usd:.2f}",
        "currency": "USD",
        "order_id": order_id,
        "lifetime": 3600,
        "description": description
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()
                return data.get("pay_url"), data.get("uuid")
    except Exception as e:
        logging.exception(f"CryptoCloud v1 create error: {e}")
    return None, None

async def cc_is_paid(invoice_uuid: str) -> bool:
    """
    Проверяет статус счёта. Возвращает True, если оплачен.
    """
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid):
        return False
    url = f"https://api.cryptocloud.plus/v2/invoice/info/{invoice_uuid}"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as resp:
                data = await resp.json()
                status = (data.get("result") or {}).get("status") or data.get("status")
                return str(status).lower() == "paid"
    except Exception as e:
        logging.exception(f"CryptoCloud check error: {e}")
        return False

# ==================== FSM ====================
class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt = State()
    media = State()
    contact = State()
    lifetime = State()     # выбор тарифа
    payment = State()      # оплата тарифа
    upsell = State()       # upsell TOP/Push
    pay_option = State()   # оплата опции (TOP/Push)

class AddBanner(StatesGroup):
    media = State()
    url = State()
    geolocation = State()
    duration = State()
    payment = State()

# ==================== KEYBOARDS ====================
def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Создать событие")],
            [KeyboardButton(text="📍 Найти события рядом")],
            [KeyboardButton(text="💰 Тарифы и продвижение")],
            [KeyboardButton(text="🖼 Купить баннер"), KeyboardButton(text="💬 Поддержка")]
        ],
        resize_keyboard=True
    )

def kb_back():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅ Назад")]], resize_keyboard=True)

def kb_categories():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎉 Вечеринка"), KeyboardButton(text="💬 Свидание")],
            [KeyboardButton(text="🧠 Встреча по интересам"), KeyboardButton(text="⚽ Активность/Спорт")],
            [KeyboardButton(text="🧭 Другое")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

def kb_media_step():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

def kb_lifetime_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌿 24ч — бесплатно", callback_data="lifetime:24")],
        [InlineKeyboardButton(text="⚡ 48ч — $%.0f" % PRICES["extend_48h"], callback_data="lifetime:48")],
        [InlineKeyboardButton(text="🌟 7 дней — $%.0f" % PRICES["extend_week"], callback_data="lifetime:168")],
        [InlineKeyboardButton(text="🚀 14 дней — $%.0f" % PRICES["extend_2week"], callback_data="lifetime:336")],
    ])

def kb_payment():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Получить ссылку на оплату")],
            [KeyboardButton(text="✅ Я оплатил")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

def kb_upsell_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ ТОП — $%.0f" % PRICES["top_week"], callback_data="upsell:top")],
        [InlineKeyboardButton(text="📡 Push 30 км — $%.0f" % PRICES["push"], callback_data="upsell:push")],
        [InlineKeyboardButton(text="🌍 Пропустить", callback_data="upsell:skip")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="upsell:back")],
    ])

def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗓 Баннер на 7 дней"), KeyboardButton(text="📅 Баннер на месяц")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

# ==================== HELPERS ====================
def format_event_card(ev: dict) -> str:
    dt = datetime.fromisoformat(ev["datetime"])
    desc = f"\n📝 {ev['description']}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {ev['contact']}" if ev.get("contact") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    return (
        f"📌 <b>{ev['title']}</b>{top}\n"
        f"📍 {ev['category']}{desc}\n"
        f"📅 {dt.strftime('%d.%m.%Y %H:%M')}{contact}"
    )

async def send_event_media(chat_id: int, ev: dict):
    text = format_event_card(ev)
    map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Google Maps", url=map_g)
    ]])
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
        await bot.send_message(chat_id, "🗺 <b>Локация:</b>", reply_markup=ikb, parse_mode="HTML")
    elif len(media) == 1:
        f = media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=ikb, parse_mode="HTML")
        elif f["type"] == "video":
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=ikb, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, text, reply_markup=ikb, parse_mode="HTML")

def banners_in_region(center_lat: float, center_lon: float, banners: List[dict], now: datetime) -> List[dict]:
    active = []
    for b in banners:
        try:
            if datetime.fromisoformat(b["expire"]) <= now:
                continue
        except Exception:
            continue
        if b.get("lat") is None or b.get("lon") is None:
            continue
        d = geodesic((center_lat, center_lon), (b["lat"], b["lon"])).km
        if d <= BANNER_REGION_RADIUS_KM:
            active.append(b)
    return active

def random_banner_for_user(user_data: dict, banners: List[dict]) -> Optional[dict]:
    now = datetime.now()
    loc = user_data.get("last_location") if user_data else None
    if loc:
        region_banners = banners_in_region(loc["lat"], loc["lon"], banners, now)
        if region_banners:
            return random.choice(region_banners)
    global_candidates = []
    for b in banners:
        try:
            if datetime.fromisoformat(b["expire"]) <= now:
                continue
        except Exception:
            continue
        if str(b.get("region", "")).lower() == "global":
            global_candidates.append(b)
    if global_candidates:
        return random.choice(global_candidates)
    return None

# ==================== START ====================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    users = _load_users()
    ukey = str(m.from_user.id)
    user_data = users.get(ukey, {})
    banners = _load_banners()
    banner = random_banner_for_user(user_data, banners)
    if banner:
        cap = (banner.get("text") or "Рекламный баннер").strip()
        url = (banner.get("url") or "").strip()
        cap_full = (cap + ("\n" + url if url else "")).strip()
        try:
            if banner.get("media_type") == "photo":
                await m.answer_photo(banner["file_id"], caption=cap_full)
            elif banner.get("media_type") == "video":
                await m.answer_video(banner["file_id"], caption=cap_full)
        except Exception as e:
            logging.warning(f"Не удалось отправить баннер: {e}")

    welcome = (
        "👋 Добро пожаловать в <b>PartyRadar</b>!\n\n"
        "🎉 Находи и создавай события: вечеринки, свидания, встречи по интересам, спорт и многое другое.\n\n"
        "📌 Объявления живут 24 часа бесплатно.\n"
        "💰 Можно выбрать платный срок, ТОП и Push при создании — всё на автомате.\n\n"
        "ℹ️ Как отправить геолокацию: нажми «скрепку» → «Геопозиция» → поставь точку на карте."
    )
    logo = None
    for ext in ("png", "jpg", "jpeg"):
        if os.path.exists(f"logo.{ext}"):
            logo = f"logo.{ext}"
            break
    if logo:
        await m.answer_photo(FSInputFile(logo), caption=welcome, reply_markup=kb_main(), parse_mode="HTML")
    else:
        await m.answer(welcome, reply_markup=kb_main(), parse_mode="HTML")

# ==================== ТАРИФЫ ====================
@dp.message(F.text == "💰 Тарифы и продвижение")
async def show_tariffs(m: Message):
    text = (
        "💰 <b>Тарифы PartyRadar</b>\n\n"
        "⏳ Сроки показа объявления:\n"
        f"• 24 часа — бесплатно\n"
        f"• 48 часов — ${PRICES['extend_48h']}\n"
        f"• 1 неделя — ${PRICES['extend_week']}\n"
        f"• 2 недели — ${PRICES['extend_2week']}\n\n"
        f"⭐ ТОП 7 дней — ${PRICES['top_week']} (первым в выдаче региона)\n"
        f"📡 Push (30 км) — ${PRICES['push']} (рассылка активным пользователям)\n"
        f"🖼 Баннер 7 дней — ${PRICES['banner_week']} / месяц — ${PRICES['banner_month']}\n\n"
        "Оплата: счёт в USD через CryptoCloud → TON/USDT (автоконверсия)."
    )
    await m.answer(text, parse_mode="HTML")

# ==================== СОЗДАНИЕ СОБЫТИЯ ====================
class AddEventData: ...

@dp.message(F.text == "➕ Создать событие")
async def create_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer("📝 Введи <b>название</b> события:", reply_markup=kb_back(), parse_mode="HTML")

@dp.message(AddEvent.title)
async def step_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await state.update_data(title=m.text.strip())
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи <b>описание</b> события (что будет, формат, условия):", reply_markup=kb_back(), parse_mode="HTML")

@dp.message(AddEvent.description)
async def step_description(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.title)
        return await m.answer("📝 Введи название события:", reply_markup=kb_back())
    await state.update_data(description=m.text.strip())
    await state.set_state(AddEvent.category)
    await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())

@dp.message(AddEvent.category)
async def step_category(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer("🧾 Введи описание события:", reply_markup=kb_back())
    await state.update_data(category=m.text.strip())
    await state.set_state(AddEvent.dt)
    await m.answer(
        "📆 Введи дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\nНапример: <code>25.10.2025 19:30</code>",
        reply_markup=kb_back(), parse_mode="HTML"
    )

@dp.message(AddEvent.dt)
async def step_datetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category)
        return await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())
    try:
        dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
        if dt <= datetime.now():
            return await m.answer("⚠ Нельзя указывать прошедшее время. Введи дату заново.", reply_markup=kb_back())
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
async def step_media(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    if len(files) >= 3:
        return await m.answer("⚠ Уже 3 файла. Отправь геолокацию, чтобы продолжить.", reply_markup=kb_media_step())

    if m.photo:
        files.append({"type": "photo", "file_id": m.photo[-1].file_id})
    elif m.video:
        files.append({"type": "video", "file_id": m.video.file_id})

    await state.update_data(media_files=files)
    left = 3 - len(files)
    tip = " Можно добавить ещё, " if left else " "
    await m.answer(
        f"✅ Файл добавлен ({len(files)}/3).{tip}или отправь геолокацию для следующего шага.",
        reply_markup=kb_media_step()
    )

@dp.message(AddEvent.media, F.content_type.in_({ContentType.VOICE, ContentType.AUDIO}))
async def media_not_supported(m: Message, state: FSMContext):
    await m.answer("⚠ Аудио и кружки не поддерживаются. Прикрепи фото/видео.", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.text == "⬅ Назад")
async def media_back(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    if files:
        files.pop()
        await state.update_data(media_files=files)
        return await m.answer(f"🗑 Удалён последний файл ({len(files)}/3).", reply_markup=kb_media_step())
    await state.set_state(AddEvent.dt)
    await m.answer("📆 Вернулись к дате/времени. Введи ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=kb_back())

@dp.message(AddEvent.media, F.location)
async def step_media_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    users = _load_users()
    users[str(m.from_user.id)] = {
        "last_location": {"lat": m.location.latitude, "lon": m.location.longitude},
        "last_seen": datetime.now().isoformat()
    }
    _save_users(users)

    await state.set_state(AddEvent.contact)
    await m.answer("☎ Укажи контакт (@username, телефон или ссылка). Или напиши «Пропустить».", reply_markup=kb_back())

@dp.message(AddEvent.contact)
async def step_contact(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer("Вернулись к медиафайлам:", reply_markup=kb_media_step())
    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=m.text.strip())
    await state.set_state(AddEvent.lifetime)
    tariff_text = (
        "📅 <b>Выбери срок жизни объявления</b>:\n\n"
        f"{LIFETIME_OPTIONS_MAP['24']['label']} — бесплатно\n"
        f"{LIFETIME_OPTIONS_MAP['48']['label']} — ${PRICES['extend_48h']}\n"
        f"{LIFETIME_OPTIONS_MAP['168']['label']} — ${PRICES['extend_week']}\n"
        f"{LIFETIME_OPTIONS_MAP['336']['label']} — ${PRICES['extend_2week']}\n\n"
        "⬇ Тапни по варианту ниже"
    )
    await m.answer(tariff_text, parse_mode="HTML", reply_markup=kb_lifetime_inline())

# ---- Выбор срока + платёж за платные сроки ----
@dp.callback_query(F.data.startswith("lifetime:"))
async def cb_lifetime(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    key = cq.data.split(":")[1]
    opt = LIFETIME_OPTIONS_MAP.get(key)
    if not opt:
        return await cq.message.answer("Тариф не найден.", reply_markup=kb_main())
    hours = opt["hours"]
    price = opt["price"]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None)

    if price == 0:
        data = await state.get_data()
        await publish_event(cq.message, state, data, hours)
        await state.set_state(AddEvent.upsell)
        return await cq.message.edit_text(
            "📢 <b>Доп. продвижение</b>\n\n"
            f"⭐ ТОП 7 дней — ${PRICES['top_week']} (первым в выдаче региона)\n"
            f"📡 Push 30 км — ${PRICES['push']} (рассылка активным пользователям)\n\n"
            "Выбери опцию или пропусти.",
            parse_mode="HTML",
            reply_markup=kb_upsell_inline()
        )
    else:
        text = (
            f"⏳ <b>Платный тариф</b>\n\n"
            f"Вы выбрали: <b>{opt['label']}</b>\n"
            f"Стоимость: <b>${price:.2f}</b>\n\n"
            "Что даёт продление:\n"
            "• дольше показ в выдаче → больше просмотров;\n"
            "• событие не исчезнет через 24 часа;\n"
            "• больше шансов собрать гостей.\n\n"
            "Нажми «💳 Получить ссылку на оплату». Счёт в USD через CryptoCloud (TON/USDT)."
        )
        await state.set_state(AddEvent.payment)
        await cq.message.edit_text(text, parse_mode="HTML")
        await cq.message.answer("Действия:", reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def lifetime_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get("paid_lifetime")
    if not hours:
        return await m.answer("❌ Нет активного платного тарифа.", reply_markup=kb_main())
    amount = TARIFFS_USD.get(hours)
    order_id = f"lifetime_{hours}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar: {hours}h lifetime")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку на оплату. Проверь .env ключи.", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == "✅ Я оплатил")
async def lifetime_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    hours = data.get("paid_lifetime")
    if not (uuid and hours):
        return await m.answer("❌ Счёт не найден. Получите ссылку ещё раз.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())
    await publish_event(m, state, data, hours)
    await state.set_state(AddEvent.upsell)
    await m.answer(
        "✅ Событие опубликовано!\n\n"
        "📢 <b>Доп. продвижение</b>\n"
        f"• ⭐ ТОП 7 дней — ${PRICES['top_week']}\n"
        f"• 📡 Push 30 км — ${PRICES['push']}\n\n"
        "Выберите опцию или разместите бесплатно.",
        parse_mode="HTML",
        reply_markup=kb_upsell_inline()
    )

@dp.message(AddEvent.payment, F.text == "⬅ Назад")
async def lifetime_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    tariff_text = (
        "📅 <b>Выбери срок жизни объявления</b>:\n\n"
        f"{LIFETIME_OPTIONS_MAP['24']['label']} — бесплатно\n"
        f"{LIFETIME_OPTIONS_MAP['48']['label']} — ${PRICES['extend_48h']}\n"
        f"{LIFETIME_OPTIONS_MAP['168']['label']} — ${PRICES['extend_week']}\n"
        f"{LIFETIME_OPTIONS_MAP['336']['label']} — ${PRICES['extend_2week']}\n\n"
        "⬇ Тапни по варианту ниже"
    )
    await m.answer(tariff_text, parse_mode="HTML", reply_markup=kb_lifetime_inline())

# ---- Дополнительные опции (ТОП / PUSH) ----
@dp.callback_query(F.data.startswith("upsell:"))
async def cb_upsell(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    action = cq.data.split(":")[1]
    data = await state.get_data()
    events = _load_events()
    my_events = [e for e in events if e["author"] == cq.from_user.id]
    if not my_events:
        await state.clear()
        return await cq.message.edit_text("❌ Не найдено созданных событий.", reply_markup=None)
    current_event = my_events[-1]

    if action == "skip":
        await state.clear()
        return await cq.message.edit_text("✅ Готово! Событие опубликовано.", reply_markup=None)

    if action == "back":
        # вернуть меню тарифов
        tariff_text = (
            "📅 <b>Выбери срок жизни объявления</b>:\n\n"
            f"{LIFETIME_OPTIONS_MAP['24']['label']} — бесплатно\n"
            f"{LIFETIME_OPTIONS_MAP['48']['label']} — ${PRICES['extend_48h']}\n"
            f"{LIFETIME_OPTIONS_MAP['168']['label']} — ${PRICES['extend_week']}\n"
            f"{LIFETIME_OPTIONS_MAP['336']['label']} — ${PRICES['extend_2week']}\n\n"
            "⬇ Тапни по варианту ниже"
        )
        await state.set_state(AddEvent.lifetime)
        return await cq.message.edit_text(tariff_text, parse_mode="HTML", reply_markup=kb_lifetime_inline())

    if action == "top":
        amount = PRICES["top_week"]
        order_id = f"top_{current_event['id']}_{cq.from_user.id}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, f"ТОП 7 дней ev#{current_event['id']}")
        if not link:
            return await cq.message.answer("⚠ Не удалось получить ссылку на оплату ТОП.")
        await state.update_data(_pay_uuid_top=uuid)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить ТОП", url=link)],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="upsell:menu")]
        ])
        await cq.message.edit_text(
            f"⭐ ТОП — ${amount}\n\nОбъявление будет первым в выдаче региона 7 дней.",
            reply_markup=kb
        )
        return

    if action == "push":
        amount = PRICES["push"]
        order_id = f"push_{current_event['id']}_{cq.from_user.id}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, f"Push 30км ev#{current_event['id']}")
        if not link:
            return await cq.message.answer("⚠ Не удалось получить ссылку на оплату Push.")
        await state.update_data(_pay_uuid_push=uuid)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить Push", url=link)],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="upsell:menu")]
        ])
        await cq.message.edit_text(
            f"📡 Push — ${amount}\n\nПуш-уведомление получат активные пользователи в радиусе 30 км.",
            reply_markup=kb
        )
        return

    if action == "menu":
        await cq.message.edit_text(
            "📢 <b>Доп. продвижение</b>\n\n"
            f"⭐ ТОП 7 дней — ${PRICES['top_week']} (первым в выдаче региона)\n"
            f"📡 Push 30 км — ${PRICES['push']} (рассылка активным пользователям)\n\n"
            "Выбери опцию или пропусти.",
            parse_mode="HTML",
            reply_markup=kb_upsell_inline()
        )

# ==================== ПУБЛИКАЦИЯ СОБЫТИЯ ====================
async def publish_event(m: Message, state: FSMContext, data: dict, hours: int):
    media_files = data.get("media_files", [])
    if not media_files:
        for ext in ("png", "jpg", "jpeg"):
            if os.path.exists(f"logo.{ext}"):
                media_files = [{"type": "photo", "file_id": f"logo.{ext}", "is_local": True}]
                break
    events = _load_events()
    expires = datetime.now() + timedelta(hours=hours)
    new_id = (events[-1]["id"] + 1) if events else 1
    ev = {
        "id": new_id,
        "author": m.from_user.id,
        "title": data.get("title"),
        "description": data.get("description"),
        "category": data.get("category"),
        "datetime": data.get("datetime"),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "media_files": media_files,
        "contact": data.get("contact"),
        "expire": expires.isoformat(),
        "notified": False,
        "is_top": False
    }
    events.append(ev)
    _save_events(events)
    await state.update_data(event_id=new_id)
    try:
        await m.answer(f"✅ Событие «{ev['title']}» создано.\nСрок: {hours} ч.", reply_markup=kb_main())
    except Exception:
        pass

# ==================== ПОИСК СОБЫТИЙ ====================
@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )
    await m.answer(
        f"📍 Отправь геолокацию для поиска (скрепка → Геопозиция → точка на карте).\n"
        f"Поиск в радиусе ~ {DEFAULT_RADIUS_KM} км.",
        reply_markup=kb
    )

@dp.message(F.location)
async def search_with_location(m: Message):
    users = _load_users()
    users[str(m.from_user.id)] = {
        "last_location": {"lat": m.location.latitude, "lon": m.location.longitude},
        "last_seen": datetime.now().isoformat()
    }
    _save_users(users)

    user_loc = (m.location.latitude, m.location.longitude)
    events = _load_events()
    now = datetime.now()
    found = []
    for ev in events:
        try:
            if datetime.fromisoformat(ev["expire"]) <= now:
                continue
        except Exception:
            continue
        dist = geodesic(user_loc, (ev["lat"], ev["lon"])).km
        if dist <= DEFAULT_RADIUS_KM:
            found.append((ev, dist))

    if not found:
        return await m.answer("😔 Событий рядом не найдено.", reply_markup=kb_main())

    found.sort(key=lambda x: ((0 if x[0].get("is_top") else 1), x[1]))

    for ev, dist in found:
        text = format_event_card(ev) + f"\n📏 Расстояние: {dist:.1f} км"
        await send_event_media(m.chat.id, ev)
        await m.answer(text, parse_mode="HTML")

# ==================== PUSH-рассылка для события ====================
async def send_push_for_event(ev: dict):
    users = _load_users()
    now = datetime.now()
    count = 0
    for uid, u in users.items():
        loc = u.get("last_location")
        ts = u.get("last_seen")
        if not (loc and ts):
            continue
        try:
            if (now - datetime.fromisoformat(ts)) > timedelta(days=30):
                continue
        except Exception:
            continue
        d = geodesic((ev["lat"], ev["lon"]), (loc["lat"], loc["lon"])).km
        if d <= DEFAULT_RADIUS_KM:
            try:
                await bot.send_message(int(uid), f"📢 Новое событие рядом: {ev['title']}")
                await send_event_media(int(uid), ev)
                count += 1
            except Exception:
                pass
    logging.info(f"Push sent to {count} users.")

# ==================== PUSH-ДЕМОН (напоминания + снятие ТОПа) ====================
async def push_daemon():
    while True:
        events = _load_events()
        now = datetime.now()
        changed = False
        for ev in events:
            if ev.get("is_top") and ev.get("top_expire"):
                try:
                    if datetime.fromisoformat(ev["top_expire"]) <= now:
                        ev["is_top"] = False
                        ev["top_expire"] = None
                        changed = True
                except Exception:
                    pass
            if ev.get("notified"):
                continue
            try:
                exp = datetime.fromisoformat(ev["expire"])
            except Exception:
                continue
            if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                ev["notified"] = True
                changed = True
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📅 +48 часов", callback_data=f"extend:{ev['id']}:48")],
                        [InlineKeyboardButton(text="🗓 +1 неделя", callback_data=f"extend:{ev['id']}:168")],
                        [InlineKeyboardButton(text="🏷 +2 недели", callback_data=f"extend:{ev['id']}:336")]
                    ]
                )
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
        await asyncio.sleep(300)

# ---------- CALLBACK ПРОДЛЕНИЯ (из напоминания) ----------
@dp.callback_query(F.data.startswith("extend:"))
async def extend_from_push(cq: CallbackQuery):
    _, ev_id, hours = cq.data.split(":")
    ev_id = int(ev_id)
    hours = int(hours)
    amount = TARIFFS_USD.get(hours)
    if amount is None:
        return await cq.answer("❌ Тариф не найден.", show_alert=True)

    order_id = f"extend_{ev_id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"Продление объявления {hours}h")
    if not link:
        return await cq.answer("⚠ Ошибка при создании ссылки", show_alert=True)

    await cq.message.answer(f"💳 Ссылка на продление: {link}\nПосле оплаты событие будет продлено автоматически.")
    await cq.answer()

# ==================== АВТО-ОЧИСТКА ====================
async def cleanup_daemon():
    while True:
        now = datetime.now()
        events = _load_events()
        updated_events = []
        for ev in events:
            try:
                if datetime.fromisoformat(ev["expire"]) > now:
                    updated_events.append(ev)
                else:
                    try:
                        await bot.send_message(ev["author"], f"🗑 Событие «{ev['title']}» истекло и удалено.")
                    except Exception:
                        pass
            except Exception:
                updated_events.append(ev)
        if len(updated_events) != len(events):
            _save_events(updated_events)

        banners = _load_banners()
        updated_banners = []
        for b in banners:
            try:
                if datetime.fromisoformat(b["expire"]) > now:
                    updated_banners.append(b)
            except Exception:
                updated_banners.append(b)
        if len(updated_banners) != len(banners):
            _save_banners(updated_banners)

        await asyncio.sleep(600)

# ==================== БАННЕРЫ (покупка) ====================
class AddBanner(StatesGroup):
    media = State()
    url = State()
    geolocation = State()
    duration = State()
    payment = State()

@dp.message(F.text == "🖼 Купить баннер")
async def banner_start(m: Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer(
        "🖼 Загрузка баннера.\nПришлите <b>фото или видео</b> баннера (текст можно указать в подписи — и/или добавить ссылку далее).",
        parse_mode="HTML", reply_markup=kb_back()
    )

@dp.message(AddBanner.media, F.content_type == ContentType.PHOTO)
async def banner_media_photo(m: Message, state: FSMContext):
    file_id = m.photo[-1].file_id
    text = (m.caption or "").strip()
    await state.update_data(b_media={"type": "photo", "file_id": file_id}, b_text=text)
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@dp.message(AddBanner.media, F.content_type == ContentType.VIDEO)
async def banner_media_video(m: Message, state: FSMContext):
    file_id = m.video.file_id
    text = (m.caption or "").strip()
    await state.update_data(b_media={"type": "video", "file_id": file_id}, b_text=text)
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@dp.message(AddBanner.media)
async def banner_media_wrong(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await m.answer("⚠ Пришлите фото или видео баннера.", reply_markup=kb_back())

@dp.message(AddBanner.url)
async def banner_url(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.media)
        return await m.answer("Пришлите фото/видео баннера.", reply_markup=kb_back())
    url = None if m.text.lower().strip() == "пропустить" else m.text.strip()
    await state.update_data(b_url=url)
    await state.set_state(AddBanner.geolocation)
    await m.answer("📍 Отправьте геолокацию региона, где показывать баннер (скрепка → Геопозиция).", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                  [KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    ))

@dp.message(AddBanner.geolocation, F.location)
async def banner_geo(m: Message, state: FSMContext):
    await state.update_data(b_lat=m.location.latitude, b_lon=m.location.longitude)
    await state.set_state(AddBanner.duration)
    await m.answer(
        f"⏳ Выберите срок показа баннера:\n"
        f"• 7 дней — ${PRICES['banner_week']}\n"
        f"• 30 дней — ${PRICES['banner_month']}",
        reply_markup=kb_banner_duration()
    )

@dp.message(AddBanner.geolocation)
async def banner_geo_wait(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.url)
        return await m.answer("🔗 Укажи ссылку (или «Пропустить»).", reply_markup=kb_back())
    await m.answer("⚠ Отправьте геолокацию баннера.", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                  [KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    ))

@dp.message(AddBanner.duration)
async def banner_duration(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.geolocation)
        return await m.answer("📍 Отправьте геолокацию региона баннера.", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                      [KeyboardButton(text="⬅ Назад")]],
            resize_keyboard=True
        ))
    if m.text == "🗓 Баннер на 7 дней":
        amount = PRICES["banner_week"]; days = 7
    elif m.text == "📅 Баннер на месяц":
        amount = PRICES["banner_month"]; days = 30
    else:
        return await m.answer("Выберите один из вариантов.", reply_markup=kb_banner_duration())

    data = await state.get_data()
    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    if b_lat is None or b_lon is None:
        await state.set_state(AddBanner.geolocation)
        return await m.answer("📍 Сначала отправьте геолокацию региона баннера.", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                      [KeyboardButton(text="⬅ Назад")]],
            resize_keyboard=True
        ))

    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, _load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer(
            "❌ Все баннерные слоты в этом регионе заняты. Попробуйте позже или другой регион.",
            reply_markup=kb_main()
        )

    await state.update_data(b_days=days, _pay_uuid=None)
    await state.set_state(AddBanner.payment)
    desc = (
        "🖼 <b>Баннер</b>\n\n"
        "Можно разместить что угодно: картинку/видео, текст, ссылки на проект, музыку, соцсети — без ограничений.\n"
        "Баннер показывается в /start пользователям в выбранном регионе (до 3 слотов на регион, ротация).\n\n"
        f"Стоимость: ${amount}\nНажмите «💳 Получить ссылку на оплату».")
    await m.answer(desc, parse_mode="HTML", reply_markup=kb_payment())

@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("b_days")
    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    if not days:
        return await m.answer("❌ Срок не выбран.", reply_markup=kb_banner_duration())

    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, _load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer(
            "❌ Все баннерные слоты в этом регионе заняты. Попробуйте позже или другой регион.",
            reply_markup=kb_main()
        )

    amount = PRICES["banner_week"] if days == 7 else PRICES["banner_month"]
    order_id = f"banner_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи.", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("❌ Счёт не найден. Получите ссылку ещё раз.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, _load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer(
            "❌ Пока вы оплачивали, все слоты заняли. Напишите в поддержку — решим вопрос.",
            reply_markup=kb_main()
        )

    b_media = data.get("b_media"); b_text = (data.get("b_text") or "").strip()
    b_url = data.get("b_url"); days = data.get("b_days")
    banners = _load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    expire = datetime.now() + timedelta(days=days)
    banners.append({
        "id": new_id,
        "owner": m.from_user.id,
        "media_type": b_media["type"],
        "file_id": b_media["file_id"],
        "text": b_text,
        "url": b_url,
        "lat": b_lat,
        "lon": b_lon,
        "region": "geo",
        "expire": expire.isoformat()
    })
    _save_banners(banners)
    await state.clear()
    await m.answer("✅ Баннер активирован и будет показан пользователям в выбранном регионе.", reply_markup=kb_main())

@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("⏳ Выберите срок показа баннера:", reply_markup=kb_banner_duration())

# ==================== SUPPORT / BACK / FALLBACK ====================
@dp.message(F.text == "💬 Поддержка")
async def support(m: Message):
    contact = os.getenv("SUPPORT_USERNAME", "@ТВОЙ_ЮЗЕР")
    await m.answer(f"📩 Поддержка: {contact}", reply_markup=kb_main())

@dp.message(F.text == "⬅ Назад")
async def global_back(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🏠 Главное меню:", reply_markup=kb_main())

@dp.message()
async def fallback(m: Message):
    await m.answer("❓ Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())

# ==================== RUN ====================
async def main():
    logging.info("✅ PartyRadar запущен…")
    asyncio.create_task(push_daemon())
    asyncio.create_task(cleanup_daemon())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
