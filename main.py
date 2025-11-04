# main.py — PartyRadar (Render-ready)
import os
import json
import logging
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ---------- LOAD CONFIG ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN required in .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Payeer merchant id (optional)
PAYEER_MERCHANT_ID = os.getenv("PAYEER_MERCHANT_ID", "").strip()

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("partyradar")

# ---------- FILES & CONSTANTS ----------
EVENTS_FILE = "events.json"
BANNERS_FILE = "banners.json"
USERS_FILE = "users.json"

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
    "📅 48 часов": 48,
    "🗓 1 неделя": 168,
    "🏷 2 недели": 336
}
TARIFFS_USD = {
    48: PRICES["extend_48h"],
    168: PRICES["extend_week"],
    336: PRICES["extend_2week"]
}

MAX_BANNERS_PER_REGION = 3
BANNER_REGION_RADIUS_KM = 30

# ---------- HELPERS: JSON storage ----------
def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception(f"Failed load {path}: {e}")
        return default

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_events() -> List[dict]:
    return _load_json(EVENTS_FILE, [])

def _save_events(events: List[dict]):
    _save_json(EVENTS_FILE, events)

def _load_banners() -> List[dict]:
    return _load_json(BANNERS_FILE, [])

def _save_banners(banners: List[dict]):
    _save_json(BANNERS_FILE, banners)

def _load_users() -> Dict[str, dict]:
    return _load_json(USERS_FILE, {})

def _save_users(users: Dict[str, dict]):
    _save_json(USERS_FILE, users)

# ---------- CRYPTOCLOUD integration (basic) ----------
async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    """Create invoice in CryptoCloud. Returns (link, uuid) on success, (None,None) otherwise."""
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
        return None, None
    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}", "Content-Type": "application/json"}
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
                return link, uuid
    except Exception as e:
        logger.exception("CryptoCloud create invoice error: %s", e)
    return None, None

async def cc_is_paid(invoice_uuid: str) -> bool:
    """Check invoice status in CryptoCloud."""
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid):
        return False
    url = f"https://api.cryptocloud.plus/v2/invoice/info/{invoice_uuid}"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as resp:
                data = await resp.json()
                status = data.get("result", {}).get("status")
                return str(status).lower() == "paid"
    except Exception as e:
        logger.exception("CryptoCloud check error: %s", e)
    return False

# ---------- AIogram bot init ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ---------- FSM states ----------
class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt = State()
    media = State()
    contact = State()
    lifetime = State()
    payment = State()
    upsell = State()
    pay_option = State()

class AddBanner(StatesGroup):
    media = State()
    url = State()
    geolocation = State()
    duration = State()
    payment = State()

# ---------- Keyboards ----------
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto, InputMediaVideo

def kb_main():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Создать событие")],
        [KeyboardButton(text="📍 Найти события рядом")],
        [KeyboardButton(text="💰 Тарифы и продвижение")],
        [KeyboardButton(text="🖼 Купить баннер"), KeyboardButton(text="💬 Поддержка")]
    ], resize_keyboard=True)

def kb_back():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅ Назад")]], resize_keyboard=True)

def kb_categories():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎉 Вечеринка"), KeyboardButton(text="💬 Свидание")],
        [KeyboardButton(text="🧠 Встреча по интересам"), KeyboardButton(text="⚽ Активность/Спорт")],
        [KeyboardButton(text="🧭 Другое")],
        [KeyboardButton(text="⬅ Назад")]
    ], resize_keyboard=True)

def kb_media_step():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
        [KeyboardButton(text="⬅ Назад")]
    ], resize_keyboard=True)

def kb_lifetime():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🕐 24 часа (бесплатно)"), KeyboardButton(text="📅 48 часов")],
        [KeyboardButton(text="🗓 1 неделя"), KeyboardButton(text="🏷 2 недели")],
        [KeyboardButton(text="⬅ Назад")]
    ], resize_keyboard=True)

def kb_payment():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💳 Получить ссылку на оплату")],
        [KeyboardButton(text="✅ Я оплатил")],
        [KeyboardButton(text="⬅ Назад")]
    ], resize_keyboard=True)

def kb_upsell():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⭐ Разместить в ТОП (7 дней)"), KeyboardButton(text="📡 Push-уведомление (30 км)")],
        [KeyboardButton(text="🌍 Разместить бесплатно (без опций)")],
        [KeyboardButton(text="⬅ Назад")]
    ], resize_keyboard=True)

def kb_banner_duration():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🗓 Баннер на 7 дней"), KeyboardButton(text="📅 Баннер на месяц")],
        [KeyboardButton(text="⬅ Назад")]
    ], resize_keyboard=True)

# ---------- Helpers ----------
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

async def send_event_media(chat_id: int, ev: dict, extra_kb=None):
    text = format_event_card(ev)
    map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
    map_tg = f"https://t.me/share/url?url={map_g}"
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📍 Telegram", url=map_tg),
        InlineKeyboardButton(text="🌐 Google Maps", url=map_g)
    ]])
    if extra_kb:
        # not adding extra_kb to group messages — will send separately
        pass
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
    from geopy.distance import geodesic
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

# ---------- START HANDLER ----------
@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    # show banner if any
    users = _load_users()
    user = users.get(str(m.from_user.id), {})
    banners = _load_banners()
    banner = None
    # choose banner near user if geo known
    if user.get("last_location"):
        banner = None
        region = banners_in_region(user["last_location"]["lat"], user["last_location"]["lon"], banners, datetime.now())
        if region:
            import random
            banner = random.choice(region)
    else:
        # fallback global banner
        for b in banners:
            if str(b.get("region", "")).lower() == "global":
                banner = b
                break
    if banner:
        if banner.get("media_type") == "photo":
            try:
                await m.answer_photo(banner["file_id"], caption=(banner.get("text","") + "\n" + (banner.get("url","") or "")).strip())
            except Exception:
                pass
        elif banner.get("media_type") == "video":
            try:
                await m.answer_video(banner["file_id"], caption=(banner.get("text","") + "\n" + (banner.get("url","") or "")).strip())
            except Exception:
                pass

    welcome = (
        "👋 Добро пожаловать в <b>PartyRadar</b>!\n\n"
        "🎉 Находи и создавай события: вечеринки, свидания, встречи по интересам, спорт и многое другое.\n\n"
        "📌 Объявления живут 24 часа бесплатно.\n"
        "💰 Можно выбрать платный срок, ТОП и Push при создании — всё на автомате."
    )
    # send logo if exists
    for ext in ("png", "jpg", "jpeg"):
        if os.path.exists(f"logo.{ext}"):
            try:
                await m.answer_photo(FSInputFile(f"logo.{ext}"), caption=welcome, reply_markup=kb_main())
                return
            except Exception:
                break
    await m.answer(welcome, reply_markup=kb_main(), parse_mode="HTML")

# ---------- CREATE EVENT FSM ----------
@dp.message(F.text == "➕ Создать событие")
async def create_start(m: types.Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer("📝 Введи <b>название</b> события:", reply_markup=kb_back(), parse_mode="HTML")

@dp.message(AddEvent.title)
async def step_title(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await state.update_data(title=m.text.strip())
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи <b>описание</b> события:", reply_markup=kb_back(), parse_mode="HTML")

@dp.message(AddEvent.description)
async def step_description(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.title)
        return await m.answer("📝 Введи название:", reply_markup=kb_back())
    await state.update_data(description=m.text.strip())
    await state.set_state(AddEvent.category)
    await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())

@dp.message(AddEvent.category)
async def step_category(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer("🧾 Введи описание:", reply_markup=kb_back())
    await state.update_data(category=m.text.strip())
    await state.set_state(AddEvent.dt)
    await m.answer("📆 Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=kb_back())

@dp.message(AddEvent.dt)
async def step_datetime(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category)
        return await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())
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

@dp.message(AddEvent.media, F.content_type.in_({"photo","video"}))
async def step_media(m: types.Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    if len(files) >= 3:
        return await m.answer("⚠ Уже 3 файла. Отправь геолокацию.", reply_markup=kb_media_step())
    if m.photo:
        files.append({"type":"photo","file_id":m.photo[-1].file_id})
    elif m.video:
        files.append({"type":"video","file_id":m.video.file_id})
    await state.update_data(media_files=files)
    left = 3 - len(files)
    await m.answer(f"✅ Файл добавлен ({len(files)}/3). " + ("Можно добавить ещё или " if left else "") + "отправь геолокацию для следующего шага.", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.location)
async def step_media_location(m: types.Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    # save user last location
    users = _load_users()
    users[str(m.from_user.id)] = {"last_location":{"lat":m.location.latitude,"lon":m.location.longitude},"last_seen":datetime.now().isoformat()}
    _save_users(users)
    await state.set_state(AddEvent.contact)
    await m.answer("☎ Укажи контакт (@username, телефон или ссылка). Или напиши «Пропустить».", reply_markup=kb_back())

@dp.message(AddEvent.contact)
async def step_contact(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer("Вернулись к медиафайлам:", reply_markup=kb_media_step())
    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=m.text.strip())
    await state.set_state(AddEvent.lifetime)
    await m.answer("⏳ Выбери срок жизни объявления:", reply_markup=kb_lifetime())

@dp.message(AddEvent.lifetime)
async def step_lifetime(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.contact)
        return await m.answer("☎ Укажи контакт или напиши «Пропустить».", reply_markup=kb_back())
    if m.text not in LIFETIME_OPTIONS:
        return await m.answer("Выбери вариант из списка:", reply_markup=kb_lifetime())

    hours = LIFETIME_OPTIONS[m.text]
    # free 24h
    if hours == 24:
        data = await state.get_data()
        await publish_event(m, state, data, hours)
        await state.set_state(AddEvent.upsell)
        await m.answer(
            "💡 Дополнительные опции монетизации:\n\n"
            "⭐ ТОП (7 дней) — показывать вверху выдачи региона.\n"
            "📡 Push (30 км) — разослать активным пользователям.\n\n"
            "Выберите опцию или разместите бесплатно.",
            reply_markup=kb_upsell()
        )
        return

    # paid path
    amount = TARIFFS_USD[hours]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None)
    description = (
        f"⏳ Платный тариф: {m.text}\nСтоимость: ${amount}\n\n"
        "Нажмите «💳 Получить ссылку на оплату» для оплаты через CryptoCloud."
    )
    await state.set_state(AddEvent.payment)
    await m.answer(description, reply_markup=kb_payment(), parse_mode="HTML")

@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def lifetime_get_link(m: types.Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get("paid_lifetime")
    if not hours:
        return await m.answer("❌ Нет активного тарифа.", reply_markup=kb_main())
    amount = TARIFFS_USD[hours]
    order_id = f"lifetime_{hours}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar: {hours}h lifetime")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку на оплату. Проверь ключи.", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == "✅ Я оплатил")
async def lifetime_paid(m: types.Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid"); hours = data.get("paid_lifetime")
    if not (uuid and hours):
        return await m.answer("❌ Нет счёта. Получите ссылку ещё раз.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())
    await publish_event(m, state, data, hours)
    await state.set_state(AddEvent.upsell)
    await m.answer("✅ Событие опубликовано! Выберите дополнительные опции:", reply_markup=kb_upsell())

@dp.message(AddEvent.payment, F.text == "⬅ Назад")
async def lifetime_back(m: types.Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    await m.answer("⏳ Вернулись к выбору срока жизни:", reply_markup=kb_lifetime())

@dp.message(AddEvent.upsell)
async def upsell_options(m: types.Message, state: FSMContext):
    text = m.text
    if text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    events = _load_events()
    my_events = [e for e in events if e["author"] == m.from_user.id]
    if not my_events:
        await state.clear()
        return await m.answer("❌ Не найдено созданных событий.", reply_markup=kb_main())
    current_event = my_events[-1]
    if text == "🌍 Разместить бесплатно (без опций)":
        await state.clear()
        return await m.answer("✅ Готово! Событие опубликовано.", reply_markup=kb_main())
    if text == "⭐ Разместить в ТОП (7 дней)":
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_type="top", opt_event_id=current_event["id"], _pay_uuid=None)
        return await m.answer(f"⭐ ТОП 7 дней — ${PRICES['top_week']}. Нажми оплатить.", reply_markup=kb_payment())
    if text == "📡 Push-уведомление (30 км)":
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_type="push", opt_event_id=current_event["id"], _pay_uuid=None)
        return await m.answer(f"📡 Push — ${PRICES['push']}. Нажми оплатить.", reply_markup=kb_payment())
    return await m.answer("Выберите опцию из меню ниже:", reply_markup=kb_upsell())

@dp.message(AddEvent.pay_option, F.text == "💳 Получить ссылку на оплату")
async def opt_get_link(m: types.Message, state: FSMContext):
    data = await state.get_data()
    opt = data.get("opt_type"); ev_id = data.get("opt_event_id")
    if not (opt and ev_id):
        return await m.answer("❌ Опция не выбрана.", reply_markup=kb_upsell())
    amount = PRICES["top_week"] if opt == "top" else PRICES["push"]
    order_id = f"{opt}_{ev_id}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar {opt}")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку на оплату.", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddEvent.pay_option, F.text == "✅ Я оплатил")
async def opt_paid(m: types.Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid"); opt = data.get("opt_type"); ev_id = data.get("opt_event_id")
    if not (uuid and opt and ev_id):
        return await m.answer("❌ Счёт не найден.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())
    events = _load_events()
    target = next((e for e in events if e["id"] == ev_id), None)
    if not target:
        await state.clear()
        return await m.answer("❌ Событие не найдено.", reply_markup=kb_main())
    if opt == "top":
        target["is_top"] = True
        target["top_expire"] = (datetime.now() + timedelta(days=7)).isoformat()
        _save_events(events)
        await m.answer("✅ ТОП активирован на 7 дней!", reply_markup=kb_upsell())
    elif opt == "push":
        await send_push_for_event(target)
        await m.answer("✅ Push-рассылка отправлена.", reply_markup=kb_upsell())

@dp.message(AddEvent.pay_option, F.text == "⬅ Назад")
async def opt_back(m: types.Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer("Выберите дополнительную опцию:", reply_markup=kb_upsell())

# ---------- Publish event ----------
async def publish_event(m: types.Message, state: FSMContext, data: dict, hours: int):
    media_files = data.get("media_files", [])
    if not media_files:
        for ext in ("png","jpg","jpeg"):
            if os.path.exists(f"logo.{ext}"):
                media_files = [{"type":"photo","file_id":f"logo.{ext}","is_local":True}]
                break
    events = _load_events()
    expires = datetime.now() + timedelta(hours=hours)
    new_id = (events[-1]["id"] + 1) if events else 1
    ev = {
        "id": new_id,
        "author": m.from_user.id,
        "title": data["title"],
        "description": data["description"],
        "category": data["category"],
        "datetime": data["datetime"],
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
    await state.clear()
    await m.answer("✅ Событие успешно создано и опубликовано!", reply_markup=kb_main())

# ---------- SEARCH ----------
@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: types.Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")]
        ], resize_keyboard=True
    )
    await m.answer(f"📍 Отправь геолокацию для поиска (радиус ~{DEFAULT_RADIUS_KM} км).\nСкрепка → Геопозиция → точка на карте.", reply_markup=kb)

@dp.message(F.location)
async def search_with_location(m: types.Message):
    users = _load_users()
    users[str(m.from_user.id)] = {"last_location":{"lat":m.location.latitude,"lon":m.location.longitude},"last_seen":datetime.now().isoformat()}
    _save_users(users)
    user_loc = (m.location.latitude, m.location.longitude)
    events = _load_events()
    now = datetime.now()
    found = []
    from geopy.distance import geodesic
    for ev in events:
        try:
            if datetime.fromisoformat(ev["expire"]) <= now:
                continue
        except Exception:
            continue
        if ev.get("lat") is None or ev.get("lon") is None:
            continue
        dist = geodesic(user_loc, (ev["lat"], ev["lon"])).km
        if dist <= DEFAULT_RADIUS_KM:
            found.append((ev, dist))
    # sort top first then distance
    found.sort(key=lambda x: ((0 if x[0].get("is_top") else 1), x[1]))
    if not found:
        return await m.answer("😔 Событий рядом не найдено.", reply_markup=kb_main())
    for ev, dist in found:
        text = format_event_card(ev) + f"\n📏 Расстояние: {dist:.1f} км"
        map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        map_tg = f"https://t.me/share/url?url={map_g}"
        ikb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📍 Telegram", url=map_tg),
            InlineKeyboardButton(text="🌐 Google Maps", url=map_g)
        ]])
        await send_event_media(m.chat.id, ev, extra_kb=ikb)

# ---------- PUSH: send push for event ----------
async def send_push_for_event(ev: dict):
    users = _load_users()
    now = datetime.now()
    count = 0
    from geopy.distance import geodesic
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
                await send_event_media(int(uid), ev)
                count += 1
            except Exception:
                pass
    logger.info(f"Push sent to {count} users.")

# ---------- Background daemons ----------
async def push_daemon():
    while True:
        events = _load_events()
        now = datetime.now()
        changed = False
        for ev in events:
            # expire top
            if ev.get("is_top") and ev.get("top_expire"):
                try:
                    if datetime.fromisoformat(ev["top_expire"]) <= now:
                        ev["is_top"] = False
                        ev["top_expire"] = None
                        changed = True
                except Exception:
                    pass
            # notify 2 hours before expire
            if ev.get("notified"):
                continue
            try:
                exp = datetime.fromisoformat(ev["expire"])
            except Exception:
                continue
            if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                ev["notified"] = True
                changed = True
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📅 +48 часов", callback_data=f"extend:{ev['id']}:48")],
                    [InlineKeyboardButton(text="🗓 +1 неделя", callback_data=f"extend:{ev['id']}:168")],
                    [InlineKeyboardButton(text="🏷 +2 недели", callback_data=f"extend:{ev['id']}:336")]
                ])
                try:
                    await bot.send_message(ev["author"], f"⏳ Событие «{ev['title']}» скоро завершится. Хотите продлить?", reply_markup=kb)
                except Exception:
                    pass
        if changed:
            _save_events(events)
        await asyncio.sleep(300)

async def cleanup_daemon():
    while True:
        now = datetime.now()
        events = _load_events()
        updated = []
        for ev in events:
            try:
                if datetime.fromisoformat(ev["expire"]) > now:
                    updated.append(ev)
                else:
                    try:
                        await bot.send_message(ev["author"], f"🗑 Событие «{ev['title']}» истекло и удалено.")
                    except Exception:
                        pass
            except Exception:
                updated.append(ev)
        if len(updated) != len(events):
            _save_events(updated)
        # banners
        banners = _load_banners()
        banners_updated = []
        for b in banners:
            try:
                if datetime.fromisoformat(b["expire"]) > now:
                    banners_updated.append(b)
            except Exception:
                banners_updated.append(b)
        if len(banners_updated) != len(banners):
            _save_banners(banners_updated)
        await asyncio.sleep(600)

# ---------- CALLBACK extend from push ----------
@dp.callback_query(F.data.startswith("extend:"))
async def extend_from_push(cq: types.CallbackQuery, state: FSMContext):
    try:
        _, ev_id, hours = cq.data.split(":")
        ev_id = int(ev_id); hours = int(hours)
    except Exception:
        return await cq.answer("Неверные данные", show_alert=True)
    amount = TARIFFS_USD.get(hours)
    if not amount:
        return await cq.answer("Тариф не найден", show_alert=True)
    await cq.message.answer(f"Продление на {hours} ч — ${amount}.\nОткройте «➕ Создать событие» и выберите платный срок.")
    await cq.answer()

# ---------- BANNERS purchase FSM ----------
@dp.message(F.text == "🖼 Купить баннер")
async def banner_start(m: types.Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer("🖼 Загрузка баннера. Пришлите фото или видео баннера (с подписью/ссылкой).", reply_markup=kb_back())

@dp.message(AddBanner.media, F.content_type == "photo")
async def banner_media_photo(m: types.Message, state: FSMContext):
    file_id = m.photo[-1].file_id
    text = (m.caption or "").strip()
    await state.update_data(b_media={"type":"photo","file_id":file_id}, b_text=text)
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@dp.message(AddBanner.media, F.content_type == "video")
async def banner_media_video(m: types.Message, state: FSMContext):
    file_id = m.video.file_id
    text = (m.caption or "").strip()
    await state.update_data(b_media={"type":"video","file_id":file_id}, b_text=text)
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@dp.message(AddBanner.url)
async def banner_url(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.media)
        return await m.answer("Пришлите фото/видео баннера.", reply_markup=kb_back())
    url = None if m.text.lower().strip() == "пропустить" else m.text.strip()
    await state.update_data(b_url=url)
    await state.set_state(AddBanner.geolocation)
    await m.answer("📍 Отправьте геолокацию региона, где показывать баннер (скрепка → Геопозиция).", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],[KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    ))

@dp.message(AddBanner.geolocation, F.location)
async def banner_geo(m: types.Message, state: FSMContext):
    await state.update_data(b_lat=m.location.latitude, b_lon=m.location.longitude)
    await state.set_state(AddBanner.duration)
    await m.answer(f"⏳ Выберите срок показа баннера:\n• 7 дней — ${PRICES['banner_week']}\n• 30 дней — ${PRICES['banner_month']}", reply_markup=kb_banner_duration())

@dp.message(AddBanner.duration)
async def banner_duration(m: types.Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddBanner.geolocation)
        return await m.answer("📍 Отправьте геолокацию региона баннера.", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],[KeyboardButton(text="⬅ Назад")]],
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
        return await m.answer("📍 Сначала отправьте геолокацию баннера.", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],[KeyboardButton(text="⬅ Назад")]],
            resize_keyboard=True
        ))
    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, _load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer("❌ Все баннерные слоты в этом регионе заняты. Попробуйте позже или другой регион.", reply_markup=kb_main())
    await state.update_data(b_days=days, _pay_uuid=None)
    await state.set_state(AddBanner.payment)
    desc = (
        f"🖼 Баннер на {days} дней — ${amount}\n"
        "Нажмите «💳 Получить ссылку на оплату»."
    )
    await m.answer(desc, reply_markup=kb_payment())

@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_get_link(m: types.Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("b_days")
    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    if not days:
        return await m.answer("❌ Срок не выбран.", reply_markup=kb_banner_duration())
    # check slots again
    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, _load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer("❌ Слоты заняты.", reply_markup=kb_main())
    amount = PRICES["banner_week"] if days == 7 else PRICES["banner_month"]
    order_id = f"banner_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку.", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: types.Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("❌ Счёт не найден.", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())
    # final slot check
    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    now = datetime.now()
    region_active = banners_in_region(b_lat, b_lon, _load_banners(), now)
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        await state.clear()
        return await m.answer("❌ Слоты уже заняты. Свяжитесь с поддержкой.", reply_markup=kb_main())
    banners = _load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    expire = datetime.now() + timedelta(days=data.get("b_days",7))
    b_media = data.get("b_media"); b_text = (data.get("b_text") or "").strip()
    b_url = data.get("b_url")
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
    await m.answer("✅ Баннер активирован.", reply_markup=kb_main())

@dp.message(F.text == "💬 Поддержка")
async def support(m: types.Message):
    await m.answer("📩 Поддержка: @ТВОЙ_ЮЗЕР", reply_markup=kb_main())

@dp.message(F.text == "⬅ Назад")
async def global_back(m: types.Message):
    await m.answer("Главное меню:", reply_markup=kb_main())

@dp.message()
async def fallback(m: types.Message):
    await m.answer("Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())

# ---------- WEB server endpoints for payments and verification ----------
async def handle_root(request):
    return web.Response(text="PartyRadar backend OK")

async def handle_payeer_file(request):
    # serve payeer verification file if present
    filename = "payeer_2272186438.txt"
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/plain")
        return web.Response(status=404, text="File not found")
    except Exception as e:
        logger.exception("payeer file serve error: %s", e)
        return web.Response(status=500, text="Error")

async def handle_payeer_notify(request):
    # Payeer will POST params to this endpoint (depending on their API)
    # We'll just log for now and set internal flags if needed.
    try:
        data = await request.post()
        logger.info("Payeer notify received: %s", dict(data))
        # You can implement signature verification here based on Payeer docs
        # For now, accept and respond OK
        return web.Response(text="OK")
    except Exception as e:
        logger.exception("Payeer notify error: %s", e)
        return web.Response(status=500, text="Error")

async def handle_cryptocloud_callback(request):
    try:
        payload = await request.json()
        logger.info("CryptoCloud callback: %s", payload)
        # payload usually contains uuid/order_id/status — you can process and activate options
        return web.Response(text="OK")
    except Exception as e:
        logger.exception("cryptocloud callback error: %s", e)
        return web.Response(status=500, text="Error")

# ---------- Start aiohttp app ----------
app = web.Application()
app.add_routes([
    web.get("/", handle_root),
    web.get("/payeer_2272186438.txt", handle_payeer_file),
    web.post("/payeer_notify", handle_payeer_notify),
    web.post("/cryptocloud_callback", handle_cryptocloud_callback),
    # optional success/fail pages
    web.get("/success", lambda r: web.Response(text="Payment success")),
    web.get("/fail", lambda r: web.Response(text="Payment failed"))
])

async def start_web_app():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    logger.info("🌍 Web server running on port 8000")

# ---------- MAIN ----------
async def main():
    logger.info("✅ PartyRadar starting...")
    # ensure json files exist
    for p in (EVENTS_FILE, BANNERS_FILE, USERS_FILE):
        if not os.path.exists(p):
            _save_json(p, {} if p.endswith("json") and p != EVENTS_FILE else ([] if p==EVENTS_FILE or p==BANNERS_FILE else {}))
    # start background daemons
    asyncio.create_task(push_daemon())
    asyncio.create_task(cleanup_daemon())
    # start webserver and bot
    await start_web_app()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("Fatal error: %s", e)
