# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import random
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
from geopy.distance import geodesic

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, InputMediaPhoto, InputMediaVideo,
    ContentType
)
from aiogram.utils.markdown import escape_html
from dotenv import load_dotenv

# ===================== CONFIG =====================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()

# Необязательные
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)  # без parse_mode здесь (aiogram>=3.10)
dp = Dispatcher(storage=MemoryStorage())

BASE_DIR: Path = Path(__file__).resolve().parent

# Файлы хранилища
EVENTS_FILE = BASE_DIR / "events.json"
BANNERS_FILE = BASE_DIR / "banners.json"
USERS_FILE = BASE_DIR / "users.json"
PAYMENTS_FILE = BASE_DIR / "payments.json"

# Базовые настройки
DEFAULT_RADIUS_KM = 30
BANNER_REGION_RADIUS_KM = 30
MAX_BANNERS_PER_REGION = 3
PUSH_LEAD_HOURS = 2

# Цены (USD)
PRICES = {
    "extend_48h": 1.0,
    "extend_week": 3.0,
    "extend_2week": 5.0,
    "top_week": 5.0,
    "push": 2.0,
    "banner_week": 10.0,
    "banner_month": 30.0,
}

# Выбор сроков (часы)
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


# ===================== HELPERS =====================
def safe(text: Optional[str]) -> str:
    return escape_html(text or "")


def _ensure_file(path: Path, default_obj: Any):
    if not path.exists():
        path.write_text(json.dumps(default_obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path, default_obj: Any) -> Any:
    try:
        _ensure_file(path, default_obj)
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_obj


def _save_json(path: Path, data: Any):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _doc_events() -> Dict[str, Any]:
    # { "events": [ { ... } ] }
    return _load_json(EVENTS_FILE, {"events": []})


def _save_doc_events(d: Dict[str, Any]):
    _save_json(EVENTS_FILE, d)


def _doc_banners() -> Dict[str, Any]:
    # { "banners": [ { ... } ] }
    return _load_json(BANNERS_FILE, {"banners": []})


def _save_doc_banners(d: Dict[str, Any]):
    _save_json(BANNERS_FILE, d)


def _doc_users() -> Dict[str, Any]:
    # { "<user_id>": { "last_location": {"lat":..., "lon":...}, "last_seen": iso } }
    return _load_json(USERS_FILE, {})


def _save_doc_users(d: Dict[str, Any]):
    _save_json(USERS_FILE, d)


def _doc_payments() -> Dict[str, Any]:
    # { "payments": [ { "uuid":..., "type": "...", "user":..., "ev_id":..., "amount":... } ] }
    return _load_json(PAYMENTS_FILE, {"payments": []})


def _save_doc_payments(d: Dict[str, Any]):
    _save_json(PAYMENTS_FILE, d)


def next_id(items: List[dict]) -> int:
    return (items[-1]["id"] + 1) if items else 1


def format_event_card(ev: dict) -> str:
    dt = datetime.fromisoformat(ev["datetime"])
    desc = f"\n📝 {safe(ev.get('description'))}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {safe(ev.get('contact'))}" if ev.get("contact") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    return (
        f"📌 <b>{safe(ev.get('title'))}</b>{top}\n"
        f"📍 {safe(ev.get('category'))}{desc}\n"
        f"📅 {dt.strftime('%d.%m.%Y %H:%M')}{contact}"
    )


def banners_in_region(center_lat: float, center_lon: float, banners: List[dict], now: datetime) -> List[dict]:
    active = []
    for b in banners:
        try:
            exp = b.get("expire")
            if exp and datetime.fromisoformat(exp) <= now:
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
        region_b = banners_in_region(loc["lat"], loc["lon"], banners, now)
        if region_b:
            return random.choice(region_b)
    # fallback: глобальные
    glob = [b for b in banners if (not b.get("lat") and not b.get("lon"))]
    if glob:
        return random.choice(glob)
    return None


async def send_event_media(chat_id: int, ev: dict):
    text = format_event_card(ev)
    map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
    map_tg = f"https://t.me/share/url?url={map_g}"
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📍 Telegram", url=map_tg),
        InlineKeyboardButton(text="🌐 Google Maps", url=map_g)
    ]])

    media = ev.get("media_files") or []
    # локальные файлы (логотип) — превращаем в FSInputFile
    fixed_media = []
    for f in media:
        if f.get("is_local"):
            fixed_media.append({"type": f["type"], "file_id": FSInputFile(str(BASE_DIR / f["file_id"]))})
        else:
            fixed_media.append(f)

    if len(fixed_media) > 1:
        group = []
        for i, f in enumerate(fixed_media):
            caption = text if i == 0 else None
            if f["type"] == "photo":
                group.append(InputMediaPhoto(media=f["file_id"], caption=caption, parse_mode=ParseMode.HTML))
            elif f["type"] == "video":
                group.append(InputMediaVideo(media=f["file_id"], caption=caption, parse_mode=ParseMode.HTML))
        await bot.send_media_group(chat_id, group)
        await bot.send_message(chat_id, "🗺 <b>Локация:</b>", reply_markup=ikb, parse_mode=ParseMode.HTML)
    elif len(fixed_media) == 1:
        f = fixed_media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=ikb, parse_mode=ParseMode.HTML)
        elif f["type"] == "video":
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=ikb, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id, text, reply_markup=ikb, parse_mode=ParseMode.HTML)


# ===================== CRYPTOCLOUD =====================
async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Создать счёт в CryptoCloud. Возвращает (link, uuid) или (None, None).
    """
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
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
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()
                link = data.get("result", {}).get("link")
                uuid = data.get("result", {}).get("uuid")
                return link, uuid
    except Exception as e:
        logging.exception(f"CryptoCloud create error: {e}")
    return None, None


async def cc_is_paid(invoice_uuid: str) -> bool:
    """
    Проверка статуса счёта. True если оплачен.
    """
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid):
        return False
    url = f"https://api.cryptocloud.plus/v2/invoice/info/{invoice_uuid}"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=30) as resp:
                data = await resp.json()
                status = (data.get("result", {}) or {}).get("status", "")
                return str(status).lower() == "paid"
    except Exception as e:
        logging.exception(f"CryptoCloud check error: {e}")
        return False


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


class AddBanner(StatesGroup):
    media = State()
    url = State()
    geolocation = State()
    duration = State()
    payment = State()


# ===================== KEYBOARDS =====================
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
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    )


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
            [KeyboardButton(text="🕐 24 часа (бесплатно)"), KeyboardButton(text="📅 48 часов")],
            [KeyboardButton(text="🗓 1 неделя"), KeyboardButton(text="🏷 2 недели")],
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
            [KeyboardButton(text="⭐ Разместить в ТОП (7 дней)"),
             KeyboardButton(text="📡 Push-уведомление (30 км)")],
            [KeyboardButton(text="🌍 Разместить бесплатно (без опций)")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗓 Баннер на 7 дней"),
             KeyboardButton(text="📅 Баннер на месяц")],
            [KeyboardButton(text="⬅ Назад")]
        ],
        resize_keyboard=True
    )


# ===================== START =====================
@dp.message(CommandStart())
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()

    # 1) Логотип всегда (если есть в корне)
    sent_logo = False
    for ext in ("png", "jpg", "jpeg"):
        p = BASE_DIR / f"logo.{ext}"
        if p.exists():
            try:
                await m.answer_photo(FSInputFile(str(p)), caption="👋 Добро пожаловать в PartyRadar!")
                sent_logo = True
                break
            except Exception as e:
                logging.warning(f"Logo send failed: {e}")
    if not sent_logo:
        await m.answer("👋 Добро пожаловать в PartyRadar!")

    # 2) Баннер региона (если есть активные)
    users = _doc_users()
    user_data = users.get(str(m.from_user.id), {})
    banners_doc = _doc_banners()
    banner = random_banner_for_user(user_data, banners_doc.get("banners", []))
    if banner:
        url = (banner.get("url") or "").strip()
        cap = (banner.get("text") or "").strip()
        cap_full = (cap + ("\n" + url if url else "")).strip()
        if banner.get("media_type") == "photo":
            await m.answer_photo(banner["file_id"], caption=cap_full)
        elif banner.get("media_type") == "video":
            await m.answer_video(banner["file_id"], caption=cap_full)

    # 3) Приветствие и меню
    welcome = (
        "🎉 Добро пожаловать в <b>PartyRadar</b>!\n\n"
        "🔥 Находи и создавай события: вечеринки, свидания, встречи по интересам, спорт и многое другое.\n\n"
        "⏳ Объявления живут <b>24 часа бесплатно</b>.\n"
        "💳 Можно выбрать платный срок, ТОП или Push — всё на автомате."
    )
    await m.answer(welcome, reply_markup=kb_main(), parse_mode=ParseMode.HTML)


# ===================== ТАРИФЫ =====================
@dp.message(F.text == "💰 Тарифы и продвижение")
async def show_tariffs(m: Message):
    text = (
        "💰 <b>Тарифы PartyRadar</b>\n\n"
        "⏳ Сроки показа объявления:\n"
        f"• 24 часа — бесплатно\n"
        f"• 48 часов — ${PRICES['extend_48h']}\n"
        f"• 1 неделя — ${PRICES['extend_week']}\n"
        f"• 2 недели — ${PRICES['extend_2week']}\n\n"
        f"⭐ ТОП 7 дней — ${PRICES['top_week']}\n"
        f"📡 Push (30 км) — ${PRICES['push']}\n"
        f"🖼 Баннер 7 дней — ${PRICES['banner_week']} / месяц — ${PRICES['banner_month']}\n\n"
        "Оплата: счёт в USD через CryptoCloud → оплата TON/USDT, авто-конверсия."
    )
    await m.answer(text, parse_mode=ParseMode.HTML)


# ===================== СОЗДАНИЕ СОБЫТИЯ =====================
@dp.message(F.text == "➕ Создать событие")
async def create_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer("📝 Введи <b>название</b> события:", reply_markup=kb_back(), parse_mode=ParseMode.HTML)


@dp.message(AddEvent.title)
async def step_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())
    await state.update_data(title=m.text.strip())
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи <b>описание</b> события:", reply_markup=kb_back(), parse_mode=ParseMode.HTML)


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
    await m.answer("📆 Введи дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\nПример: 25.12.2025 19:30",
                   reply_markup=kb_back(), parse_mode=ParseMode.HTML)


@dp.message(AddEvent.dt)
async def step_datetime(m: Message, state: FSMContext):
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
        "🧭 Как выбрать точку на карте: скрепка → Геопозиция → «Выбрать на карте».\n"
        "⚠ Аудио/кружки не поддерживаются.",
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
    tail = " Можно добавить ещё." if left else " Достигнут лимит — отправь геолокацию."
    await m.answer(f"✅ Файл добавлен ({len(files)}/3).{tail}", reply_markup=kb_media_step())


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

    # Запомним локацию для пушей/баннеров
    users = _doc_users()
    users[str(m.from_user.id)] = {
        "last_location": {"lat": m.location.latitude, "lon": m.location.longitude},
        "last_seen": datetime.now().isoformat()
    }
    _save_doc_users(users)

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
        await publish_event(m, state, data, hours)
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "💡 Дополнительные опции:\n"
            "⭐ <b>ТОП на 7 дней</b> — событие показываетcя первым в выдаче региона.\n"
            "📡 <b>Push (30 км)</b> — рассылка всем активным пользователям рядом.\n\n"
            "Выберите опцию или разместите бесплатно.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_upsell()
        )

    # Платные сроки — счёт
    amount = TARIFFS_USD[hours]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None)
    description = (
        f"⏳ <b>Платный тариф</b>\n\n"
        f"Вы выбрали: <b>{m.text}</b>\n"
        f"Стоимость: <b>${amount}</b>\n\n"
        "Что вы получаете:\n"
        "• дольше показ в выдаче → больше просмотров;\n"
        "• событие не исчезнет через 24 часа;\n"
        "• больше шансов собрать гостей.\n\n"
        "Нажмите «💳 Получить ссылку на оплату» для оплаты через CryptoCloud."
    )
    await state.set_state(AddEvent.payment)
    await m.answer(description, parse_mode=ParseMode.HTML, reply_markup=kb_payment())


@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def lifetime_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get("paid_lifetime")
    if not hours:
        return await m.answer("❌ Нет активного платного тарифа.", reply_markup=kb_payment())
    amount = TARIFFS_USD[hours]
    order_id = f"lifetime_{hours}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar: {hours}h lifetime")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку на оплату. Проверь .env ключи.", reply_markup=kb_payment())
    # сохраним платеж
    pays = _doc_payments()
    pays["payments"].append({"uuid": uuid, "type": "lifetime", "user": m.from_user.id, "amount": amount, "hours": hours})
    _save_doc_payments(pays)

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

    # публикуем событие
    await publish_event(m, state, data, hours)
    await state.set_state(AddEvent.upsell)
    await m.answer(
        "✅ Событие опубликовано!\n\n"
        f"⭐ ТОП 7 дней — ${PRICES['top_week']}\n"
        f"📡 Push (30 км) — ${PRICES['push']}\n\n"
        "Выберите опцию или разместите бесплатно.",
        reply_markup=kb_upsell()
    )


@dp.message(AddEvent.payment, F.text == "⬅ Назад")
async def lifetime_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    await m.answer("⏳ Вернулись к выбору срока жизни объявления:", reply_markup=kb_lifetime())


@dp.message(AddEvent.upsell)
async def upsell_options(m: Message, state: FSMContext):
    text = m.text
    if text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    events_doc = _doc_events()
    my_events = [e for e in events_doc["events"] if e["author"] == m.from_user.id]
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
        return await m.answer(
            f"⭐ <b>ТОП на 7 дней</b> — событие показывается первым в регионе.\n"
            f"Стоимость: ${PRICES['top_week']}\n\n"
            "Нажмите «💳 Получить ссылку на оплату».",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_payment()
        )

    if text == "📡 Push-уведомление (30 км)":
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_type="push", opt_event_id=current_event["id"], _pay_uuid=None)
        return await m.answer(
            f"📡 <b>Push-уведомление</b> — сообщение получат активные пользователи в радиусе 30 км от точки события.\n"
            f"Стоимость: ${PRICES['push']}\n\n"
            "Нажмите «💳 Получить ссылку на оплату».",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_payment()
        )

    return await m.answer("Выберите опцию из меню ниже:", reply_markup=kb_upsell())


@dp.message(AddEvent.pay_option, F.text == "💳 Получить ссылку на оплату")
async def opt_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    opt = data.get("opt_type")
    ev_id = data.get("opt_event_id")
    if not (opt and ev_id):
        return await m.answer("❌ Опция не выбрана.", reply_markup=kb_upsell())

    amount = PRICES["top_week"] if opt == "top" else PRICES["push"]
    order_id = f"{opt}_{ev_id}_{m.from_user.id}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar {opt}")
    if not link:
        return await m.answer("⚠ Не удалось получить ссылку на оплату. Проверь .env ключи.", reply_markup=kb_payment())

    pays = _doc_payments()
    pays["payments"].append({"uuid": uuid, "type": opt, "user": m.from_user.id, "ev_id": ev_id, "amount": amount})
    _save_doc_payments(pays)

    await state.update_data(_pay_uuid=uuid)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())


@dp.message(AddEvent.pay_option, F.text == "✅ Я оплатил")
async def opt_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    opt = data.get("opt_type")
    ev_id = data.get("opt_event_id")
    if not (uuid and opt and ev_id):
        return await m.answer("❌ Счёт не найден. Получите ссылку ещё раз.", reply_markup=kb_payment())

    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())

    events_doc = _doc_events()
    target = next((e for e in events_doc["events"] if e["id"] == ev_id), None)
    if not target:
        await state.clear()
        return await m.answer("❌ Событие не найдено.", reply_markup=kb_main())

    if opt == "top":
        target["is_top"] = True
        target["top_expire"] = (datetime.now() + timedelta(days=7)).isoformat()
        _save_doc_events(events_doc)
        await m.answer("✅ ТОП активирован на 7 дней!", reply_markup=kb_upsell())
    elif opt == "push":
        await send_push_for_event(target)
        await m.answer("✅ Push-рассылка отправлена активным пользователям в радиусе 30 км.", reply_markup=kb_upsell())


@dp.message(AddEvent.pay_option, F.text == "⬅ Назад")
async def opt_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer("Выберите дополнительную опцию:", reply_markup=kb_upsell())


async def publish_event(m: Message, state: FSMContext, data: dict, hours: int):
    media_files = data.get("media_files", [])
    if not media_files:
        # подставим лого, если есть
        for ext in ("png", "jpg", "jpeg"):
            p = BASE_DIR / f"logo.{ext}"
            if p.exists():
                media_files = [{"type": "photo", "file_id": f"logo.{ext}", "is_local": True}]
                break

    events_doc = _doc_events()
    items = events_doc["events"]
    expires = datetime.now() + timedelta(hours=hours)
    new_id = next_id(items)
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
    items.append(ev)
    _save_doc_events(events_doc)

    await state.update_data(last_event_id=new_id)
    await m.answer("✅ Событие успешно создано и опубликовано!", reply_markup=kb_main())


# ===================== ПОИСК СОБЫТИЙ =====================
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
        f"📍 Отправь геолокацию для поиска.\n"
        f"🧭 Скрепка → Геопозиция → «Выбрать на карте».\n"
        f"Поиск в радиусе ~ {DEFAULT_RADIUS_KM} км.",
        reply_markup=kb
    )


@dp.message(F.location)
async def search_with_location(m: Message):
    # Сохраним локацию пользователя (для пушей/баннеров)
    users = _doc_users()
    users[str(m.from_user.id)] = {
        "last_location": {"lat": m.location.latitude, "lon": m.location.longitude},
        "last_seen": datetime.now().isoformat()
    }
    _save_doc_users(users)

    user_loc = (m.location.latitude, m.location.longitude)
    events_doc = _doc_events()
    events = events_doc.get("events", [])
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

    # ТОП вперёд, затем по расстоянию
    found.sort(key=lambda x: ((0 if x[0].get("is_top") else 1), x[1]))

    if not found:
        ikb_none = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Создать событие", callback_data="go_create")
        ]])
        return await m.answer("😔 Событий рядом не найдено.\nМожешь создать своё!", reply_markup=kb_main(),
                              parse_mode=ParseMode.HTML)

    for ev, dist in found:
        text = format_event_card(ev) + f"\n📏 Расстояние: {dist:.1f} км"
        map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        map_tg = f"https://t.me/share/url?url={map_g}"
        ikb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📍 Telegram", url=map_tg),
            InlineKeyboardButton(text="🌐 Google Maps", url=map_g)
        ]])

        media = ev.get("media_files") or []
        for f in media:
            if f.get("is_local"):
                f["file_id"] = FSInputFile(str(BASE_DIR / f["file_id"]))

        if len(media) > 1:
            group = []
            for i, f in enumerate(media):
                caption = text if i == 0 else None
                if f["type"] == "photo":
                    group.append(InputMediaPhoto(media=f["file_id"], caption=caption, parse_mode=ParseMode.HTML))
                elif f["type"] == "video":
                    group.append(InputMediaVideo(media=f["file_id"], caption=caption, parse_mode=ParseMode.HTML))
            await bot.send_media_group(m.chat.id, group)
            await bot.send_message(m.chat.id, "🗺 <b>Локация:</b>", reply_markup=ikb, parse_mode=ParseMode.HTML)
        elif len(media) == 1:
            f = media[0]
            if f["type"] == "photo":
                await m.answer_photo(f["file_id"], caption=text, reply_markup=ikb, parse_mode=ParseMode.HTML)
            elif f["type"] == "video":
                await m.answer_video(f["file_id"], caption=text, reply_markup=ikb, parse_mode=ParseMode.HTML)
        else:
            await m.answer(text, reply_markup=ikb, parse_mode=ParseMode.HTML)


# ===================== PUSH =====================
async def send_push_for_event(ev: dict):
    users = _doc_users()
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
                await send_event_media(int(uid), ev)
                count += 1
            except Exception:
                pass
    logging.info(f"Push sent to {count} users.")


async def push_daemon():
    while True:
        events_doc = _doc_events()
        changed = False
        now = datetime.now()
        for ev in events_doc["events"]:
            # снять просроченный ТОП
            if ev.get("is_top") and ev.get("top_expire"):
                try:
                    if datetime.fromisoformat(ev["top_expire"]) <= now:
                        ev["is_top"] = False
                        ev["top_expire"] = None
                        changed = True
                except Exception:
                    pass
            # уведомление за 2 часа
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
                        f"⏳ Событие «{safe(ev['title'])}» скоро завершится. Хотите продлить?",
                        reply_markup=kb
                    )
                except Exception:
                    pass
        if changed:
            _save_doc_events(events_doc)
        await asyncio.sleep(300)


@dp.callback_query(F.data.startswith("extend:"))
async def extend_from_push(cq: CallbackQuery):
    _, ev_id, hours = cq.data.split(":")
    ev_id = int(ev_id)
    hours = int(hours)
    amount = TARIFFS_USD.get(hours)
    if not amount:
        return await cq.answer("Тариф не найден", show_alert=True)
    await cq.message.answer(
        f"Продление на {hours} ч — ${amount}.\n"
        "Откройте «➕ Создать событие» и выберите платный срок для публикации/продления.",
    )
    await cq.answer()


# ===================== БАННЕРЫ =====================
@dp.message(F.text == "🖼 Купить баннер")
async def banner_start(m: Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer(
        "🖼 Загрузка баннера.\nПришлите <b>фото или видео</b> баннера (подпись можно добавить — и ссылку далее).",
        parse_mode=ParseMode.HTML, reply_markup=kb_back()
    )


@dp.message(AddBanner.media, F.content_type == ContentType.PHOTO)
async def banner_media_photo(m: Message, state: FSMContext):
    await state.update_data(b_media={"type": "photo", "file_id": m.photo[-1].file_id},
                            b_text=(m.caption or "").strip())
    await state.set_state(AddBanner.url)
    await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())


@dp.message(AddBanner.media, F.content_type == ContentType.VIDEO)
async def banner_media_video(m: Message, state: FSMContext):
    await state.update_data(b_media={"type": "video", "file_id": m.video.file_id},
                            b_text=(m.caption or "").strip())
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
    await m.answer("📍 Отправьте геолокацию региона, где показывать баннер (скрепка → Геопозиция).",
                   reply_markup=ReplyKeyboardMarkup(
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

    # Проверка слотов
    banners_doc = _doc_banners()
    region_active = banners_in_region(b_lat, b_lon, banners_doc.get("banners", []), datetime.now())
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
    await m.answer(desc, parse_mode=ParseMode.HTML, reply_markup=kb_payment())


@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("b_days")
    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    if not days:
        return await m.answer("❌ Срок не выбран.", reply_markup=kb_banner_duration())

    banners_doc = _doc_banners()
    region_active = banners_in_region(b_lat, b_lon, banners_doc.get("banners", []), datetime.now())
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

    pays = _doc_payments()
    pays["payments"].append({"uuid": uuid, "type": "banner", "user": m.from_user.id,
                             "amount": amount, "days": days, "lat": b_lat, "lon": b_lon})
    _save_doc_payments(pays)

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

    b_media = data.get("b_media"); b_text = (data.get("b_text") or "").strip()
    b_url = data.get("b_url"); days = data.get("b_days")
    b_lat = data.get("b_lat"); b_lon = data.get("b_lon")

    # Повторная проверка слотов
    banners_doc = _doc_banners()
    region_active = banners_in_region(b_lat, b_lon, banners_doc.get("banners", []), datetime.now())
    if len(region_active) >= MAX_BANNERS_PER_REGION:
        return await m.answer(
            "❌ Пока вы оплачивали, все слоты заняли. Напишите в поддержку — решим вопрос.",
            reply_markup=kb_main()
        )

    items = banners_doc["banners"]
    new_id = next_id(items)
    expire = datetime.now() + timedelta(days=days)
    items.append({
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
    _save_doc_banners(banners_doc)
    await state.clear()
    await m.answer("✅ Баннер активирован и будет показан пользователям в выбранном регионе.", reply_markup=kb_main())


@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("⏳ Выберите срок показа баннера:", reply_markup=kb_banner_duration())


# ===================== SUPPORT / BACK / FALLBACK =====================
@dp.message(F.text == "💬 Поддержка")
async def support(m: Message):
    await m.answer("📩 Поддержка: @ТВОЙ_ЮЗЕР", reply_markup=kb_main())


@dp.message(F.text == "⬅ Назад")
async def global_back(m: Message, state: FSMContext):
    # Универсальный «Назад» — возвращаем в главное меню
    # (внутри шагов уже есть контекстный «Назад», этот — глобальная защита)
    await state.clear()
    await m.answer("Главное меню:", reply_markup=kb_main())


@dp.message()
async def fallback(m: Message):
    await m.answer("Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())


# ===================== CLEANUP DAEMON =====================
async def cleanup_daemon():
    while True:
        now = datetime.now()

        # События
        events_doc = _doc_events()
        before = len(events_doc["events"])
        events_doc["events"] = [
            ev for ev in events_doc["events"]
            if not ev.get("expire") or datetime.fromisoformat(ev["expire"]) > now
        ]
        after = len(events_doc["events"])
        if after != before:
            _save_doc_events(events_doc)

        # Баннеры
        banners_doc = _doc_banners()
        before_b = len(banners_doc["banners"])
        banners_doc["banners"] = [
            b for b in banners_doc["banners"]
            if not b.get("expire") or datetime.fromisoformat(b["expire"]) > now
        ]
        after_b = len(banners_doc["banners"])
        if after_b != before_b:
            _save_doc_banners(banners_doc)

        await asyncio.sleep(600)


# ===================== RUN =====================
async def main():
    logging.info("✅ PartyRadar запущен...")
    asyncio.create_task(push_daemon())
    asyncio.create_task(cleanup_daemon())
    await dp.start_polling(bot)


async def safe_run():
    while True:
        try:
            await main()
        except Exception as e:
            logging.error(f"❌ Polling crashed: {e}")
            await asyncio.sleep(5)
            logging.info("♻️ Restarting polling...")


if __name__ == "__main__":
    try:
        asyncio.run(safe_run())
    except (KeyboardInterrupt, SystemExit):
        logging.info("🛑 Bot stopped manually.")
