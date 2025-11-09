# PartyRadar — main.py (Final v2.1)
# Все правки включены: Google Maps only, Back на каждом шаге, CryptoCloud, баннеры, пуши, автоочистка

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from geopy.distance import geodesic

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, ContentType,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo
)

# -------------------- CONFIG --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # например https://partyradar.onrender.com

assert BOT_TOKEN, "BOT_TOKEN пуст"
assert PUBLIC_URL, "PUBLIC_URL пуст (https://<ваш-домен>)"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())

# -------------------- FILES/CONSTS --------------------
EVENTS_FILE = "events.json"
USERS_FILE = "users.json"
BANNERS_FILE = "banners.json"
PENDING_FILE = "pending.json"  # ожидания оплат (uuid→payload)

DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2

PRICES = {
    "extend_48h": 1.0,
    "extend_week": 3.0,
    "extend_2week": 5.0,
    "top_week": 5.0,
    "push": 2.0,
    "banner_week": 10.0,
    "banner_month": 30.0,
}

LIFETIME_OPTIONS = {
    "🕐 24 часа (бесплатно)": 24,
    "📅 48 часов (+$1)": 48,
    "🗓 1 неделя (+$3)": 168,
    "🏷 2 недели (+$5)": 336
}
TARIFFS_USD = {48: PRICES["extend_48h"], 168: PRICES["extend_week"], 336: PRICES["extend_2week"]}

MAX_BANNERS_PER_REGION = 3
BANNER_REGION_RADIUS_KM = 30

# -------------------- STORAGE UTILS --------------------
def _load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_events(): return _load(EVENTS_FILE, [])
def save_events(d): _save(EVENTS_FILE, d)
def load_users(): return _load(USERS_FILE, {})
def save_users(d): _save(USERS_FILE, d)
def load_banners(): return _load(BANNERS_FILE, [])
def save_banners(d): _save(BANNERS_FILE, d)
def load_pending(): return _load(PENDING_FILE, {})
def save_pending(d): _save(PENDING_FILE, d)

# -------------------- FSM --------------------
class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt = State()
    media = State()
    contact = State()
    lifetime = State()
    pay_lifetime = State()
    upsell = State()
    pay_option = State()

class AddBanner(StatesGroup):
    media = State()
    url = State()
    geolocation = State()
    duration = State()
    payment = State()

# -------------------- KEYBOARDS --------------------
def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Создать событие")],
            [KeyboardButton(text="📍 Найти события рядом")],
            [KeyboardButton(text="🖼 Купить баннер"), KeyboardButton(text="💰 Тарифы")],
            [KeyboardButton(text="💬 Поддержка")]
        ],
        resize_keyboard=True
    )

def kb_back(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅ Назад")]], resize_keyboard=True)

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

def kb_lifetime():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕐 24 часа (бесплатно)"), KeyboardButton(text="📅 48 часов (+$1)")],
            [KeyboardButton(text="🗓 1 неделя (+$3)"), KeyboardButton(text="🏷 2 недели (+$5)")],
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
            [KeyboardButton(text="⭐ ТОП на 7 дней"), KeyboardButton(text="📡 Push (30 км)")],
            [KeyboardButton(text="🌍 Разместить бесплатно (без опций)")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗓 Баннер на 7 дней"), KeyboardButton(text="📅 Баннер на месяц")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )

# -------------------- HELPERS --------------------
async def typewriter(chat_id: int, text: str, delay: float = 0.015):
    msg = await bot.send_message(chat_id, "​")
    out = ""
    for ch in text:
        out += ch
        await asyncio.sleep(delay)
        try:
            await bot.edit_message_text(out, chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass

def format_event_card(ev: dict) -> str:
    dt = datetime.fromisoformat(ev["datetime"])
    desc = f"\n📝 {ev['description']}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {ev['contact']}" if ev.get("contact") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    return f"📌 <b>{ev['title']}</b>{top}\n📍 {ev['category']}{desc}\n📅 {dt.strftime('%d.%m.%Y %H:%M')}{contact}"

async def send_media_card(chat_id: int, ev: dict):
    text = format_event_card(ev)
    map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
    ikb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть на карте", url=map_g)]
    ])
    media = ev.get("media_files") or []
    for f in media:
        if f.get("is_local"):
            f["file_id"] = FSInputFile(f["file_id"])
    if len(media) > 1:
        group = []
        for i, f in enumerate(media):
            cap = text if i == 0 else None
            if f["type"] == "photo":
                group.append(InputMediaPhoto(media=f["file_id"], caption=cap, parse_mode="HTML"))
            elif f["type"] == "video":
                group.append(InputMediaVideo(media=f["file_id"], caption=cap, parse_mode="HTML"))
        await bot.send_media_group(chat_id, group)
        await bot.send_message(chat_id, "🗺 Локация:", reply_markup=ikb)
    elif len(media) == 1:
        f = media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=ikb)
        elif f["type"] == "video":
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=ikb)
    else:
        await bot.send_message(chat_id, text, reply_markup=ikb)

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

def pick_random_banner_for_user(user_data: dict, banners: List[dict]) -> Optional[dict]:
    now = datetime.now()
    loc = user_data.get("last_location") if user_data else None
    if loc:
        reg = banners_in_region(loc["lat"], loc["lon"], banners, now)
        if reg:
            import random
            return random.choice(reg)
    globals_ = [b for b in banners if b.get("region") == "global" and datetime.fromisoformat(b["expire"]) > now]
    if globals_:
        import random
        return random.choice(globals_)
    return None

# -------------------- CRYPTOCLOUD --------------------
async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    if not (CRYPTOCLOUD_API_KEY and CRYPTOCLOUD_SHOP_ID):
        return None, None
    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": float(amount_usd),
        "currency": "USD",
        "order_id": order_id,
        "description": description,
        "locale": "ru",
        "success_url": f"{PUBLIC_URL}/payment_success",
        "fail_url": f"{PUBLIC_URL}/payment_fail",
        "callback_url": f"{PUBLIC_URL}/payment_callback"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json=payload, timeout=30) as r:
                data = await r.json()
                link = data.get("result", {}).get("link")
                uuid = data.get("result", {}).get("uuid")
                return link, uuid
    except Exception as e:
        logging.exception(f"cc_create_invoice error: {e}")
    return None, None

async def cc_is_paid(uuid: str) -> bool:
    if not (uuid and CRYPTOCLOUD_API_KEY):
        return False
    url = f"https://api.cryptocloud.plus/v2/invoice/info/{uuid}"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=30) as r:
                data = await r.json()
                return str(data.get("result", {}).get("status", "")).lower() == "paid"
    except Exception as e:
        logging.exception(f"cc_is_paid error: {e}")
        return False

# -------------------- START --------------------
@dp.message_handler(commands=['start'])
async def start_command(message: types.Message):
    # Логотип
    logo_path = "imgonline-com-ua-Resize-poVtNXt7aue6.png"
    with open(logo_path, 'rb') as photo:
        await bot.send_photo(message.chat.id, photo)

    # Задержка 1 секунда для эффекта
    await asyncio.sleep(1)

    # Приветствие по буквам
    welcome_text = "🎉 Добро пожаловать в PartyRadar!\n\n" \
                   "Здесь ты можешь найти вечеринки, знакомства и события рядом 🌍"
    sent_msg = await message.answer("")
    for i in range(1, len(welcome_text) + 1):
        await asyncio.sleep(0.04)
        await sent_msg.edit_text(welcome_text[:i])

    # Основное меню
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📍 Найти события рядом", callback_data="find_nearby"),
        InlineKeyboardButton("🎈 Создать событие", callback_data="create_event")
    )
    keyboard.add(
        InlineKeyboardButton("💬 Чат по радиусу", callback_data="chat_radius"),
        InlineKeyboardButton("ℹ️ О проекте", callback_data="about")
    )

    await message.answer("Главное меню:", reply_markup=keyboard)

# -------------------- ТАРИФЫ --------------------
@dp.message(F.text == "💰 Тарифы")
async def tariffs(m: Message):
    text = (
        "💰 <b>Тарифы</b>\n\n"
        "⏳ Сроки объявления:\n"
        "• 24 часа — бесплатно\n"
        f"• 48 часов — ${PRICES['extend_48h']}\n"
        f"• 1 неделя — ${PRICES['extend_week']}\n"
        f"• 2 недели — ${PRICES['extend_2week']}\n\n"
        f"⭐ ТОП 7 дней — ${PRICES['top_week']}\n"
        f"📡 Push (30 км) — ${PRICES['push']}\n"
        f"🖼 Баннер 7 дней — ${PRICES['banner_week']} / месяц — ${PRICES['banner_month']}\n\n"
        "Оплата через CryptoCloud (USD), TON/USDT — авто-конверсия."
    )
    await m.answer(text)

# -------------------- СОЗДАНИЕ СОБЫТИЯ --------------------
@dp.message(F.text == "➕ Создать событие")
async def create_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer("📝 Введи <b>название</b> события:", reply_markup=kb_back())

@dp.message(AddEvent.title)
async def step_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await state.update_data(title=m.text.strip())
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи <b>описание</b> события:", reply_markup=kb_back())

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
        "📆 Введи дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\nПример: 25.12.2025 19:30",
        reply_markup=kb_back()
    )

@dp.message(AddEvent.dt)
async def step_dt(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category)
        return await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())
    try:
        dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
        if dt <= datetime.now():
            return await m.answer("⚠ Нельзя указывать прошедшее время.", reply_markup=kb_back())
    except ValueError:
        return await m.answer("⚠ Неверный формат. Пример: 25.12.2025 19:30", reply_markup=kb_back())
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
    tail = " Можно добавить ещё или отправь геолокацию." if left else " Отправь геолокацию."
    await m.answer(f"✅ Файл добавлен ({len(files)}/3).{tail}", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO_NOTE}))
async def media_not_supported(m: Message, state: FSMContext):
    await m.answer("⚠ Аудио/кружки не поддерживаются. Прикрепи фото/видео.", reply_markup=kb_media_step())

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
async def step_media_geo(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    # запоминаем гео для пользователя (push и баннеры)
    users = load_users()
    users[str(m.from_user.id)] = {
        "last_location": {"lat": m.location.latitude, "lon": m.location.longitude},
        "last_seen": datetime.now().isoformat()
    }
    save_users(users)
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
    await m.answer("⏳ Выбери срок жизни объявления:", reply_markup=kb_lifetime())

@dp.message(AddEvent.lifetime)
async def step_lifetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.contact)
        return await m.answer("☎ Укажи контакт или напиши «Пропустить».", reply_markup=kb_back())

    if m.text not in LIFETIME_OPTIONS:
        return await m.answer("Выбери вариант из списка:", reply_markup=kb_lifetime())

    hours = LIFETIME_OPTIONS[m.text]
    if hours == 24:
        data = await state.get_data()
        await finalize_publish_event(m, state, data, hours)
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "💡 Доп.опции:\n"
            "⭐ <b>ТОП 7 дней</b> — событие будет первым в выдаче региона.\n"
            "📡 <b>Push (30 км)</b> — рассылка всем активным рядом.\n\n"
            "Выберите опцию или разместите бесплатно.",
            reply_markup=kb_upsell()
        )

    amount = TARIFFS_USD[hours]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None)
    desc = (
        f"⏳ Платный срок: <b>{hours} ч</b>\n"
        f"Стоимость: <b>${amount}</b>\n\n"
        "Нажмите «💳 Получить ссылку на оплату». После оплаты — «✅ Я оплатил»."
    )
    await state.set_state(AddEvent.pay_lifetime)
    await m.answer(desc, reply_markup=kb_payment())

@dp.message(AddEvent.pay_lifetime, F.text == "💳 Получить ссылку на оплату")
async def lifetime_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get("paid_lifetime")
    if not hours:
        return await m.answer("❌ Нет активного платного тарифа.", reply_markup=kb_lifetime())
    amount = TARIFFS_USD[hours]
    order_id = f"lifetime_{hours}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar lifetime {hours}h")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи.", reply_markup=kb_payment())
    # запишем в pending
    pend = load_pending()
    pend[uuid] = {"type": "lifetime", "user_id": m.from_user.id, "hours": hours}
    save_pending(pend)
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddEvent.pay_lifetime, F.text == "✅ Я оплатил")
async def lifetime_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    hours = data.get("paid_lifetime")
    if not (uuid and hours):
        return await m.answer("❌ Счёт не найден. Нажмите «💳 Получить ссылку на оплату».", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    # всё ок — публикуем
    await finalize_publish_event(m, state, data, hours)
    # чистим pending
    pend = load_pending()
    pend.pop(uuid, None)
    save_pending(pend)

    await state.set_state(AddEvent.upsell)
    await m.answer(
        "✅ Событие опубликовано!\n\n"
        f"⭐ ТОП 7 дней — ${PRICES['top_week']}\n"
        f"📡 Push (30 км) — ${PRICES['push']}\n\n"
        "Выберите опцию или разместите бесплатно.",
        reply_markup=kb_upsell()
    )

@dp.message(AddEvent.pay_lifetime, F.text == "⬅ Назад")
async def lifetime_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    await m.answer("⏳ Вернулись к выбору срока:", reply_markup=kb_lifetime())

# ---- Upsell (TOP / PUSH) ----
@dp.message(AddEvent.upsell)
async def upsell_opts(m: Message, state: FSMContext):
    txt = m.text
    if txt == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    events = load_events()
    my = [e for e in events if e["author"] == m.from_user.id]
    if not my:
        await state.clear()
        return await m.answer("❌ Не найдено созданных событий.", reply_markup=kb_main())
    curr = my[-1]

    if txt == "🌍 Разместить бесплатно (без опций)":
        await state.clear()
        return await m.answer("✅ Готово! Событие опубликовано.", reply_markup=kb_main())

    if txt == "⭐ ТОП на 7 дней":
        amount = PRICES["top_week"]
        order_id = f"top_{curr['id']}_{m.from_user.id}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, "PartyRadar TOP 7d")
        if not link:
            return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи.", reply_markup=kb_upsell())
        pend = load_pending()
        pend[uuid] = {"type": "top", "user_id": m.from_user.id, "event_id": curr["id"]}
        save_pending(pend)
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_uuid=uuid, opt_type="top", opt_event_id=curr["id"])
        return await m.answer(f"💳 Ссылка на оплату ТОП:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

    if txt == "📡 Push (30 км)":
        amount = PRICES["push"]
        order_id = f"push_{curr['id']}_{m.from_user.id}_{int(datetime.now().timestamp())}"
        link, uuid = await cc_create_invoice(amount, order_id, "PartyRadar Push")
        if not link:
            return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи.", reply_markup=kb_upsell())
        pend = load_pending()
        pend[uuid] = {"type": "push", "user_id": m.from_user.id, "event_id": curr["id"]}
        save_pending(pend)
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_uuid=uuid, opt_type="push", opt_event_id=curr["id"])
        return await m.answer(f"💳 Ссылка на оплату Push:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

    await m.answer("Выберите опцию из меню:", reply_markup=kb_upsell())

@dp.message(AddEvent.pay_option, F.text == "✅ Я оплатил")
async def opt_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("opt_uuid")
    opt = data.get("opt_type")
    ev_id = data.get("opt_event_id")
    if not (uuid and opt and ev_id):
        return await m.answer("❌ Счёт не найден. Попробуйте снова.", reply_markup=kb_upsell())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    events = load_events()
    target = next((e for e in events if e["id"] == ev_id), None)
    if not target:
        await state.clear()
        return await m.answer("❌ Событие не найдено.", reply_markup=kb_main())

    if opt == "top":
        target["is_top"] = True
        target["top_expire"] = (datetime.now() + timedelta(days=7)).isoformat()
        save_events(events)
        await m.answer("✅ ТОП активирован на 7 дней!", reply_markup=kb_upsell())
    elif opt == "push":
        await send_push_for_event(target)
        await m.answer("✅ Push-рассылка отправлена активным пользователям в радиусе 30 км.", reply_markup=kb_upsell())

    # чистим pending
    pend = load_pending()
    pend.pop(uuid, None)
    save_pending(pend)

@dp.message(AddEvent.pay_option, F.text == "⬅ Назад")
async def opt_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer("Выберите опцию:", reply_markup=kb_upsell())

# -------------------- ПУБЛИКАЦИЯ --------------------
async def finalize_publish_event(m: Message, state: FSMContext, data: dict, hours: int):
    media_files = data.get("media_files", [])
    if not media_files:
        # дефолт — логотип
        logo = "imgonline-com-ua-Resize-poVtNXt7aue6.png"
        if os.path.exists(logo):
            media_files = [{"type": "photo", "file_id": logo, "is_local": True}]
    events = load_events()
    new_id = (events[-1]["id"] + 1) if events else 1
    expires = datetime.now() + timedelta(hours=hours)
    ev = {
        "id": new_id,
        "author": m.from_user.id,
        "title": data["title"],
        "description": data["description"],
        "category": data["category"],
        "datetime": data["datetime"],
        "lat": data["lat"],
        "lon": data["lon"],
        "media_files": media_files,
        "contact": data.get("contact"),
        "expire": expires.isoformat(),
        "notified": False,
        "is_top": False,
        "top_expire": None
    }
    events.append(ev)
    save_events(events)
    await m.answer("✅ Событие опубликовано!", reply_markup=kb_main())
    await state.update_data(last_event_id=new_id)

# -------------------- ПОИСК --------------------
@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message, state: FSMContext):
    await m.answer(
        f"📍 Отправь геолокацию для поиска (скрепка → Геопозиция → точка на карте).\nРадиус ~ {DEFAULT_RADIUS_KM} км.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                      [KeyboardButton(text="⬅ Назад")]],
            resize_keyboard=True
        )
    )
    await state.set_state("search_geo")

@dp.message(F.state == "search_geo", F.location)
async def do_search(m: Message, state: FSMContext):
    # запомним локацию пользователя
    users = load_users()
    users[str(m.from_user.id)] = {
        "last_location": {"lat": m.location.latitude, "lon": m.location.longitude},
        "last_seen": datetime.now().isoformat()
    }
    save_users(users)

    user_loc = (m.location.latitude, m.location.longitude)
    now = datetime.now()
    events = load_events()
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

    # ТОП выше
    found.sort(key=lambda x: ((0 if x[0].get("is_top") else 1), x[1]))

    if not found:
        await m.answer("😔 Событий рядом не найдено.\nХочешь создать своё? Нажми «➕ Создать событие».", reply_markup=kb_main())
        await state.clear()
        return

    for ev, dist in found:
        text = format_event_card(ev) + f"\n📏 Расстояние: {dist:.1f} км"
        map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Открыть на карте", url=map_g)]])
        media = ev.get("media_files") or []
        for f in media:
            if f.get("is_local"):
                f["file_id"] = FSInputFile(f["file_id"])
        if len(media) > 1:
            group = []
            for i, f in enumerate(media):
                cap = text if i == 0 else None
                if f["type"] == "photo":
                    group.append(InputMediaPhoto(media=f["file_id"], caption=cap, parse_mode="HTML"))
                elif f["type"] == "video":
                    group.append(InputMediaVideo(media=f["file_id"], caption=cap, parse_mode="HTML"))
            await bot.send_media_group(m.chat.id, group)
            await bot.send_message(m.chat.id, "🗺 Локация:", reply_markup=ikb)
        elif len(media) == 1:
            f = media[0]
            if f["type"] == "photo":
                await m.answer_photo(f["file_id"], caption=text, reply_markup=ikb)
            elif f["type"] == "video":
                await m.answer_video(f["file_id"], caption=text, reply_markup=ikb)
        else:
            await m.answer(text, reply_markup=ikb)
    await state.clear()

@dp.message(F.state == "search_geo")
async def search_wait_geo(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await m.answer("⚠ Отправьте геолокацию для поиска.", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                  [KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    ))

# -------------------- PUSH / CLEANUP --------------------
async def send_push_for_event(ev: dict):
    users = load_users()
    now = datetime.now()
    sent = 0
    for uid, u in users.items():
        loc = u.get("last_location"); ts = u.get("last_seen")
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
                await send_media_card(int(uid), ev)
                sent += 1
            except Exception:
                pass
    logging.info(f"Push sent: {sent} users")

async def reminders_daemon():
    while True:
        events = load_events()
        now = datetime.now()
        changed = False
        for ev in events:
            # снять ТОП по истечении
            if ev.get("is_top") and ev.get("top_expire"):
                try:
                    if datetime.fromisoformat(ev["top_expire"]) <= now:
                        ev["is_top"] = False
                        ev["top_expire"] = None
                        changed = True
                except Exception:
                    pass
            # предупредить за 2 часа до истечения
            if not ev.get("notified"):
                try:
                    exp = datetime.fromisoformat(ev["expire"])
                except Exception:
                    continue
                if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                    ev["notified"] = True
                    changed = True
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📅 +48 часов ($1)", callback_data=f"extend:{ev['id']}:48")],
                        [InlineKeyboardButton(text="🗓 +1 неделя ($3)", callback_data=f"extend:{ev['id']}:168")],
                        [InlineKeyboardButton(text="🏷 +2 недели ($5)", callback_data=f"extend:{ev['id']}:336")]
                    ])
                    try:
                        await bot.send_message(ev["author"], f"⏳ Событие «{ev['title']}» скоро завершится. Продлить?", reply_markup=kb)
                    except Exception:
                        pass
        if changed:
            save_events(events)
        await asyncio.sleep(300)

@dp.callback_query(F.data.startswith("extend:"))
async def extend_from_push(cq: CallbackQuery):
    _, ev_id, hours = cq.data.split(":")
    ev_id = int(ev_id); hours = int(hours)
    amount = TARIFFS_USD.get(hours)
    if not amount:
        return await cq.answer("Тариф не найден", show_alert=True)
    await cq.message.answer(
        f"Продление на {hours} ч — ${amount}.\n"
        "Откройте «➕ Создать событие» → выберите платный срок (временная оплата через CryptoCloud)."
    )
    await cq.answer()

async def cleanup_daemon():
    while True:
        now = datetime.now()
        # events
        events = load_events()
        keep = []
        for ev in events:
            try:
                if datetime.fromisoformat(ev["expire"]) > now:
                    keep.append(ev)
                else:
                    try:
                        await bot.send_message(ev["author"], f"🗑 Событие «{ev['title']}» истекло и удалено.")
                    except Exception:
                        pass
            except Exception:
                keep.append(ev)
        if len(keep) != len(events):
            save_events(keep)

        # banners
        banners = load_banners()
        keep_b = []
        for b in banners:
            try:
                if datetime.fromisoformat(b["expire"]) > now:
                    keep_b.append(b)
            except Exception:
                keep_b.append(b)
        if len(keep_b) != len(banners):
            save_banners(keep_b)

        await asyncio.sleep(600)

# -------------------- БАННЕРЫ --------------------
@dp.message(F.text == "🖼 Купить баннер")
async def banner_start(m: Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer(
        "🖼 Пришлите <b>фото или видео</b> баннера. Текст добавьте в подпись (опционально).",
        reply_markup=kb_back()
    )

@dp.message(AddBanner.media, F.content_type == ContentType.PHOTO)
async def banner_media_photo(m: Message, state: FSMContext):
    fid = m.photo[-1].file_id
    text = (m.caption or "").strip()
    await state.update_data(b_media={"type": "photo", "file_id": fid}, b_text=text)
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@dp.message(AddBanner.media, F.content_type == ContentType.VIDEO)
async def banner_media_video(m: Message, state: FSMContext):
    fid = m.video.file_id
    text = (m.caption or "").strip()
    await state.update_data(b_media={"type": "video", "file_id": fid}, b_text=text)
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@dp.message(AddBanner.media)
async def banner_media_wait(m: Message, state: FSMContext):
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
    await m.answer(
        "📍 Отправьте геолокацию региона показа (скрепка → Геопозиция).",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                      [KeyboardButton(text="⬅ Назад")]],
            resize_keyboard=True
        )
    )

@dp.message(AddBanner.geolocation, F.location)
async def banner_geo_ok(m: Message, state: FSMContext):
    await state.update_data(b_lat=m.location.latitude, b_lon=m.location.longitude)
    await state.set_state(AddBanner.duration)
    await m.answer(
        f"⏳ Срок показа баннера:\n"
        f"• 7 дней — ${PRICES['banner_week']}\n"
        f"• 30 дней — ${PRICES['banner_month']}",
        reply_markup=kb_banner_duration()
    )

@dp.message(AddBanner.geolocation)
async def banner_geo_wait(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.url)
        return await m.answer("🔗 Укажи ссылку (или «Пропустить»).", reply_markup=kb_back())
    await m.answer("⚠ Отправьте геолокацию региона баннера.", reply_markup=ReplyKeyboardMarkup(
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
    if m.text not in {"🗓 Баннер на 7 дней", "📅 Баннер на месяц"}:
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
    days = 7 if "7 дней" in m.text else 30
    amount = PRICES["banner_week"] if days == 7 else PRICES["banner_month"]

    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer("❌ Все баннерные слоты в этом регионе заняты. Попробуйте позже или другой регион.", reply_markup=kb_main())

    order_id = f"banner_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи.", reply_markup=kb_banner_duration())

    # сохраняем в pending
    pend = load_pending()
    pend[uuid] = {
        "type": "banner", "user_id": m.from_user.id, "days": days,
        "lat": b_lat, "lon": b_lon
    }
    # временно сохраняем media/text/url в pending (чтобы оформить после вебхука/подтверждения)
    pend[uuid].update({
        "media": data.get("b_media"), "text": data.get("b_text"), "url": data.get("b_url")
    })
    save_pending(pend)

    await state.set_state(AddBanner.payment)
    await state.update_data(b_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("b_uuid")
    if not uuid:
        return await m.answer("❌ Счёт не найден. Получите ссылку ещё раз.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    pend = load_pending()
    payload = pend.get(uuid)
    if not payload:
        return await m.answer("❌ Данные оплаты не найдены.", reply_markup=kb_main())

    # финальная проверка слотов
    now = datetime.now()
    region_active = banners_in_region(payload["lat"], payload["lon"], load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer("❌ Пока вы оплачивали, слоты заняли. Напишите в поддержку.", reply_markup=kb_main())

    banners = load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    expire = datetime.now() + timedelta(days=payload["days"])
    banners.append({
        "id": new_id,
        "owner": m.from_user.id,
        "media_type": payload["media"]["type"],
        "file_id": payload["media"]["file_id"],
        "text": payload.get("text") or "",
        "url": payload.get("url"),
        "lat": payload["lat"], "lon": payload["lon"],
        "region": "geo",
        "expire": expire.isoformat()
    })
    save_banners(banners)
    pend.pop(uuid, None)
    save_pending(pend)

    await state.clear()
    await m.answer("✅ Баннер активирован и будет показан пользователям в выбранном регионе.", reply_markup=kb_main())

@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("⏳ Выберите срок показа баннера:", reply_markup=kb_banner_duration())

# -------------------- SUPPORT / BACK / FALLBACK --------------------
@dp.message(F.text == "💬 Поддержка")
async def support(m: Message):
    await m.answer("📩 Поддержка: @ТВОЙ_ЮЗЕР", reply_markup=kb_main())

@dp.message(F.text == "⬅ Назад")
async def global_back(m: Message):
    await m.answer("Главное меню:", reply_markup=kb_main())

@dp.message()
async def fallback(m: Message):
    await m.answer("Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())

# -------------------- WEBHOOK / CALLBACK --------------------
async def handle_payment_callback(request: web.Request) -> web.Response:
    """
    CryptoCloud webhook (POST). Тело: JSON. Ищем uuid и статус paid.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False})

    status = str(data.get("status", "")).lower()
    uuid = data.get("invoice", {}).get("uuid") or data.get("uuid")
    if not uuid:
        return web.json_response({"ok": False})

    if status == "paid":
        pend = load_pending()
        payload = pend.get(uuid)
        if not payload:
            return web.json_response({"ok": True})

        if payload["type"] == "lifetime":
            # ничего не делаем здесь — публикация уже была/будет по нажатию кнопки "Я оплатил"
            pass
        elif payload["type"] == "top":
            # не используем здесь, у нас покупка ТОПа через "Я оплатил"
            pass
        elif payload["type"] == "push":
            pass
        elif payload["type"] == "banner":
            # можно было бы сразу создавать баннер, но оставим логику через кнопку подтверждения
            pass

        return web.json_response({"ok": True})
    return web.json_response({"ok": True})

async def payment_success(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def payment_fail(request: web.Request) -> web.Response:
    return web.Response(text="FAIL")

# -------------------- RUN --------------------
async def main():
    app = web.Application()
    # webhook для бота
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    SimpleRequestHandler(dp, bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # роуты для криптоклауда
    app.router.add_post("/payment_callback", handle_payment_callback)
    app.router.add_get("/payment_success", payment_success)
    app.router.add_get("/payment_fail", payment_fail)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
    await site.start()

    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info(f"Webhook set: {WEBHOOK_URL}")

    # демоны
    asyncio.create_task(reminders_daemon())
    asyncio.create_task(cleanup_daemon())

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Stopped.")
