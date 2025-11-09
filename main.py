# -*- coding: utf-8 -*-
"""
PartyRadar — main.py (боевой режим, CryptoCloud)
Aiogram v3 + aiohttp.
Готово для Render: если есть переменная PORT — запускается webhook-сервер.
Иначе — локально через polling.

Включено:
- Приветствие с логотипом (imgonline-com-ua-Resize-poVtNXt7aue6.png) и задержкой
- Создание события (название, описание, категория, дата/время, медиа ≤3, гео, контакт, срок)
- Поиск событий по гео (30 км по умолчанию)
- Купон баннер (отдельный FSM), баннер показывается при /start по региону (30 км)
- Кнопка «Назад» на каждом шаге возвращает к предыдущему
- CryptoCloud: создание инвойса + ручная проверка + вебхук /payment_callback
- Авто-пуш за 2 часа до истечения, авто-очистка истёкших событий/баннеров
- Стабильные тексты (HTML) без «ломаных» символов
"""
import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ContentType,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = (os.getenv("CRYPTOCLOUD_API_KEY") or "").strip()
CRYPTOCLOUD_SHOP_ID = (os.getenv("CRYPTOCLOUD_SHOP_ID") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip()  # например: https://partyradar.onrender.com
DEFAULT_RADIUS_KM = int(os.getenv("DEFAULT_RADIUS_KM", "30"))
PUSH_LEAD_HOURS = 2

PRICES = {"extend_48h": 1.0,"extend_week": 3.0,"extend_2week": 5.0,"top_week": 5.0,"push": 2.0,"banner_week": 10.0,"banner_month": 30.0}
LIFETIME_OPTIONS = {"⏳ 24 часа (бесплатно)": 24,"🗓️ 48 часов": 48,"🗓️ 1 неделя": 168,"🗓️ 2 недели": 336}
BASE_DIR = Path(__file__).resolve().parent
EVENTS_FILE = BASE_DIR / "events.json"
BANNERS_FILE = BASE_DIR / "banners.json"
USERS_FILE = BASE_DIR / "users.json"
PAYMENTS_FILE = BASE_DIR / "payments.json"
LOGO_FILENAME = "imgonline-com-ua-Resize-poVtNXt7aue6.png"

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ===================== HELPERS =====================
def _ensure_file(p: Path, default):
    if not p.exists():
        p.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_json(p: Path, default):
    _ensure_file(p, default)
    try:
        text = p.read_text(encoding="utf-8")
        return json.loads(text) if text else default
    except Exception:
        return default

def _save_json(p: Path, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def doc_events() -> Dict:
    d = _load_json(EVENTS_FILE, {"events": []}); d.setdefault("events", []); return d

def save_events(d: Dict):
    d.setdefault("events", []); _save_json(EVENTS_FILE, d)

def doc_banners() -> Dict:
    d = _load_json(BANNERS_FILE, {"banners": []}); d.setdefault("banners", []); return d

def save_banners(d: Dict):
    d.setdefault("banners", []); _save_json(BANNERS_FILE, d)

def doc_users() -> Dict:
    d = _load_json(USERS_FILE, {"users": {}}); d.setdefault("users", {}); return d

def save_users(d: Dict):
    d.setdefault("users", {}); _save_json(USERS_FILE, d)

def doc_payments() -> Dict:
    d = _load_json(PAYMENTS_FILE, {"payments": []}); d.setdefault("payments", []); return d

def save_payments(d: Dict):
    d.setdefault("payments", []); _save_json(PAYMENTS_FILE, d)

def next_id(items: List[dict]) -> int:
    return (max([int(x.get("id", 0) or 0) for x in items]) + 1) if items else 1

def km_distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    from geopy.distance import geodesic
    return geodesic((a[0], a[1]), (b[0], b[1])).km

# ===================== PAYMENTS: CryptoCloud =====================
async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    if not (CRYPTOCLOUD_API_KEY and CRYPTOCLOUD_SHOP_ID): return None, None
    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}", "Content-Type": "application/json"}
    payload = {"shop_id": CRYPTOCLOUD_SHOP_ID,"amount": float(amount_usd),"currency": "USD","order_id": order_id,"description": description,"locale": "ru",
               "success_url": f"{PUBLIC_URL}/payment_callback?status=paid&order_id={order_id}",
               "fail_url": f"{PUBLIC_URL}/payment_callback?status=failed&order_id={order_id}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json=payload, timeout=30) as r:
                data = await r.json()
                return data.get("result", {}).get("link"), data.get("result", {}).get("uuid")
    except Exception as e:
        logging.exception(f"cc_create_invoice error: {e}"); return None, None

async def cc_is_paid(invoice_uuid: str) -> bool:
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid): return False
    url = f"https://api.cryptocloud.plus/v2/invoice/info/{invoice_uuid}"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=30) as r:
                data = await r.json(); status = str(data.get("result", {}).get("status", "")).lower()
                return status == "paid"
    except Exception as e:
        logging.exception(f"cc_is_paid error: {e}"); return False

def payments_add(order_id: str, user_id: int, invoice_uuid: str, amount: float, ptype: str, payload: dict):
    d = doc_payments()
    d["payments"].append({"order_id": order_id,"user_id": user_id,"uuid": invoice_uuid,"amount": amount,"ptype": ptype,"payload": payload,"status": "pending","created": datetime.utcnow().isoformat()})
    save_payments(d)

def payments_set_status(order_id: str, status: str):
    d = doc_payments()
    for p in d["payments"]:
        if p["order_id"] == order_id:
            p["status"] = status; p["updated"] = datetime.utcnow().isoformat()
    save_payments(d)

def payments_get(order_id: str) -> Optional[dict]:
    d = doc_payments()
    for p in d["payments"]:
        if p["order_id"] == order_id:
            return p
    return None

# ===================== FSM =====================
class AddEvent(StatesGroup):
    title = State(); description = State(); category = State(); dt = State(); media = State()
    location = State(); contact = State(); lifetime = State(); pay = State(); upsell = State(); upsell_pay = State()

class AddBanner(StatesGroup):
    media = State(); url = State(); geolocation = State(); duration = State(); payment = State()

# ===================== KEYBOARDS =====================
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="➕ Создать событие")],[KeyboardButton(text="📍 Найти события рядом")],[KeyboardButton(text="💰 Тарифы и продвижение")],[KeyboardButton(text="🖼 Купить баннер"), KeyboardButton(text="💬 Поддержка")]],
        resize_keyboard=True)

def kb_back() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅ Назад")]], resize_keyboard=True)

def kb_categories() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎉 Вечеринка"), KeyboardButton(text="💬 Свидание")],[KeyboardButton(text="🧠 Встреча по интересам"), KeyboardButton(text="⚽ Активность/Спорт")],[KeyboardButton(text="🧭 Другое")],[KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True)

def kb_media_step() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],[KeyboardButton(text="⬅ Назад")]],resize_keyboard=True)

def kb_lifetime() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏳ 24 часа (бесплатно)"), KeyboardButton(text="🗓️ 48 часов")],[KeyboardButton(text="🗓️ 1 неделя"), KeyboardButton(text="🗓️ 2 недели")],[KeyboardButton(text="⬅ Назад")]],resize_keyboard=True)

def kb_payment() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🧾 Получить ссылку на оплату")],[KeyboardButton(text="✅ Я оплатил")],[KeyboardButton(text="⬅ Назад")]],resize_keyboard=True)

def kb_upsell() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⭐ ТОП на 7 дней"), KeyboardButton(text="📡 Push (30 км)")],[KeyboardButton(text="🌍 Разместить без доп.опций")],[KeyboardButton(text="⬅ Назад")]],resize_keyboard=True)

def kb_banner_duration() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🗓️ Баннер на 7 дней"), KeyboardButton(text="🗓️ Баннер на месяц")],[KeyboardButton(text="⬅ Назад")]],resize_keyboard=True)

def kb_search_geo() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],[KeyboardButton(text="⬅ Назад")]],resize_keyboard=True)

# ===================== TEMPLATES =====================
def format_event_card(ev: dict) -> str:
    dt = datetime.fromisoformat(ev["datetime"])
    desc = f"\n📝 {ev['description']}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {ev['contact']}" if ev.get("contact") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    return f"📌 <b>{ev['title']}</b>{top}\n📍 {ev['category']}{desc}\n📅 {dt.strftime('%d.%m.%Y %H:%M')}{contact}"

async def send_event_media(chat_id: int, ev: dict):
    text = format_event_card(ev)
    map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
    map_tg = f"https://t.me/share/url?url={map_g}"
    ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📍 Telegram", url=map_tg),InlineKeyboardButton(text="🌐 Google Maps", url=map_g)]])
    media = ev.get("media_files") or []
    for f in media:
        if f.get("is_local"):
            f["file_id"] = FSInputFile(f["file_id"])
    if len(media) > 1:
        from aiogram.types import InputMediaPhoto, InputMediaVideo
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

# ===================== START =====================
router = Router(); dp.include_router(router)

@router.message(CommandStart())
async def start_cmd(m: Message):
    # баннер (по последней гео)
    users = doc_users(); u = users["users"].get(str(m.from_user.id), {})
    banners_doc = doc_banners(); pick = None; now = datetime.utcnow()
    if "last_location" in u:
        lat, lon = u["last_location"]["lat"], u["last_location"]["lon"]
        region = []
        for b in banners_doc["banners"]:
            try:
                if datetime.fromisoformat(b["expire"]) <= now: continue
            except Exception:
                continue
            if b.get("lat") is None or b.get("lon") is None: continue
            if km_distance((lat, lon), (b["lat"], b["lon"])) <= 30:
                region.append(b)
        if region: pick = random.choice(region)
    if not pick:
        glob = [b for b in banners_doc["banners"] if str(b.get("region", "")) == "global" and datetime.fromisoformat(b["expire"]) > now] if banners_doc["banners"] else []
        if glob: pick = random.choice(glob)
    if pick:
        cap = (pick.get("text") or "").strip(); url = (pick.get("url") or "").strip()
        if url: cap = (cap + "\n" + url).strip()
        try:
            if pick.get("media_type") == "photo":
                await m.answer_photo(pick["file_id"], caption=cap or "Рекламный баннер")
            elif pick.get("media_type") == "video":
                await m.answer_video(pick["file_id"], caption=cap or "Рекламный баннер")
        except Exception: pass

    # логотип
    logo_path = BASE_DIR / LOGO_FILENAME
    if logo_path.exists():
        try:
            await m.answer_photo(FSInputFile(str(logo_path)))
            await asyncio.sleep(0.6)
        except Exception: pass

    welcome = ("👋 Добро пожаловать в <b>PartyRadar</b>!\n\n"
               "🎉 Находи и создавай события: вечеринки, свидания, встречи по интересам, спорт и многое другое.\n\n"
               "📌 Объявления живут <b>24 часа бесплатно</b>.\n"
               "💰 Можно выбрать платный срок, ТОП и Push при создании — всё на автомате.")
    await m.answer(welcome, reply_markup=kb_main())

@router.message(F.text == "💬 Поддержка")
async def support(m: Message):
    await m.answer("📩 Поддержка: @ТВОЙ_ЮЗЕР", reply_markup=kb_main())

# ===================== CREATE EVENT =====================
@router.message(F.text == "➕ Создать событие")
async def create_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title); await m.answer("📝 Введи <b>название</b> события:", reply_markup=kb_back())

@router.message(AddEvent.title)
async def step_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear(); return await m.answer("Главное меню:", reply_markup=kb_main())
    await state.update_data(title=m.text.strip())
    await state.set_state(AddEvent.description)
    await m.answer("🧾 Введи <b>описание</b> события:", reply_markup=kb_back())

@router.message(AddEvent.description)
async def step_description(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.title); return await m.answer("📝 Введи название:", reply_markup=kb_back())
    await state.update_data(description=m.text.strip())
    await state.set_state(AddEvent.category)
    await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())

@router.message(AddEvent.category)
async def step_category(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description); return await m.answer("🧾 Введи описание:", reply_markup=kb_back())
    await state.update_data(category=m.text.strip())
    await state.set_state(AddEvent.dt)
    await m.answer("📆 Введи дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\nПример: 25.12.2025 19:30", reply_markup=kb_back())

@router.message(AddEvent.dt)
async def step_datetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category); return await m.answer("🧭 Выбери категорию:", reply_markup=kb_categories())
    try:
        dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
        if dt <= datetime.now(): return await m.answer("⚠ Нельзя указывать прошедшее время.", reply_markup=kb_back())
    except ValueError:
        return await m.answer("⚠ Неверный формат. Пример: 25.12.2025 19:30", reply_markup=kb_back())
    await state.update_data(datetime=dt.isoformat(), media_files=[])
    await state.set_state(AddEvent.media)
    await m.answer("📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n📍 Скрепка → Геопозиция → точка на карте.\n⚠ Аудио/кружки не поддерживаются.", reply_markup=kb_media_step())

@router.message(AddEvent.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def step_media(m: Message, state: FSMContext):
    data = await state.get_data(); files = data.get("media_files", [])
    if len(files) >= 3: return await m.answer("⚠ Уже 3 файла. Отправь геолокацию.", reply_markup=kb_media_step())
    if m.photo: files.append({"type": "photo", "file_id": m.photo[-1].file_id})
    elif m.video: files.append({"type": "video", "file_id": m.video.file_id})
    await state.update_data(media_files=files)
    left = 3 - len(files)
    tail = " Можно добавить ещё или " if left else " "
    await m.answer(f"✅ Файл добавлен ({len(files)}/3).{tail}отправь геолокацию для следующего шага.", reply_markup=kb_media_step())

@router.message(AddEvent.media, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE}))
async def media_not_supported(m: Message, state: FSMContext):
    await m.answer("⚠ Аудио и кружки не поддерживаются. Прикрепи фото/видео.", reply_markup=kb_media_step())

@router.message(AddEvent.media, F.text == "⬅ Назад")
async def media_back(m: Message, state: FSMContext):
    data = await state.get_data(); files = data.get("media_files", [])
    if files:
        files.pop(); await state.update_data(media_files=files)
        return await m.answer(f"🗑 Удалён последний файл ({len(files)}/3).", reply_markup=kb_media_step())
    await state.set_state(AddEvent.dt); return await m.answer("📆 Вернулись к дате/времени. Введи ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=kb_back())

@router.message(AddEvent.media, F.location)
async def step_media_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    users = doc_users(); users["users"][str(m.from_user.id)] = {"last_location": {"lat": m.location.latitude, "lon": m.location.longitude},"last_seen": datetime.utcnow().isoformat()}
    save_users(users)
    await state.set_state(AddEvent.contact)
    await m.answer("☎ Укажи контакт (@username, телефон или ссылка). Или напиши «Пропустить».", reply_markup=kb_back())

@router.message(AddEvent.contact)
async def step_contact(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media); return await m.answer("Вернулись к медиафайлам:", reply_markup=kb_media_step())
    if m.text.lower().strip() != "пропустить": await state.update_data(contact=m.text.strip())
    await state.set_state(AddEvent.lifetime); await m.answer("⏳ Выбери срок жизни объявления:", reply_markup=kb_lifetime())

@router.message(AddEvent.lifetime)
async def step_lifetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.contact); return await m.answer("☎ Укажи контакт или напиши «Пропустить».", reply_markup=kb_back())
    text = m.text.replace("️", "")
    mapping = {"⏳ 24 часа (бесплатно)": 24,"🗓 24 часа (бесплатно)": 24,"24 (бесплатно)": 24,"⏳ 24 часа бесплатно": 24,"🗓️ 48 часов": 48,"🗓 48 часов": 48,"48 часов": 48,"🗓️ 1 неделя": 168,"🗓 1 неделя": 168,"1 неделя": 168,"🗓️ 2 недели": 336,"🗓 2 недели": 336,"2 недели": 336}
    hours = mapping.get(text)
    if not hours: return await m.answer("Выбери вариант из списка:", reply_markup=kb_lifetime())
    data = await state.get_data()
    if hours == 24:
        await publish_event(m, state, data, hours)
        await state.set_state(AddEvent.upsell)
        return await m.answer("💡 Дополнительные опции:\n• ⭐ ТОП 7 дней — ${}\n• 📡 Push (30 км) — ${}\n\nВыберите опцию или разместите бесплатно.".format(PRICES["top_week"], PRICES["push"]), reply_markup=kb_upsell())
    amount = {48: PRICES["extend_48h"], 168: PRICES["extend_week"], 336: PRICES["extend_2week"]}[hours]
    order_id = f"lifetime_{hours}_{m.from_user.id}_{int(datetime.utcnow().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar lifetime {hours}h")
    if not link: return await m.answer("⚠ Не удалось получить ссылку на оплату. Проверь .env ключи и PUBLIC_URL.", reply_markup=kb_payment())
    payments_add(order_id, m.from_user.id, uuid, amount, "lifetime", {"hours": hours, "data": data})
    await state.update_data(_order_id=order_id, _uuid=uuid, _hours=hours)
    await state.set_state(AddEvent.pay)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@router.message(AddEvent.pay, F.text == "✅ Я оплатил")
async def lifetime_paid(m: Message, state: FSMContext):
    data = await state.get_data(); order_id = data.get("_order_id"); uuid = data.get("_uuid"); hours = data.get("_hours")
    if not (order_id and uuid and hours): return await m.answer("❌ Счёт не найден. Попробуйте снова получить ссылку.", reply_markup=kb_payment())
    if not await cc_is_paid(uuid): return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())
    payments_set_status(order_id, "paid")
    payload = payments_get(order_id).get("payload", {}); ev_data = payload.get("data", {})
    await publish_event(m, state, ev_data, hours)
    await state.set_state(AddEvent.upsell)
    await m.answer("✅ Событие опубликовано!\n\n💡 Дополнительные опции:\n• ⭐ ТОП 7 дней — ${}\n• 📡 Push (30 км) — ${}\n\nВыберите опцию или разместите бесплатно.".format(PRICES["top_week"], PRICES["push"]), reply_markup=kb_upsell())

@router.message(AddEvent.pay, F.text == "🧾 Получить ссылку на оплату")
async def lifetime_get_link_again(m: Message, state: FSMContext):
    await m.answer("Ссылка уже была выдана выше. После оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@router.message(AddEvent.pay, F.text == "⬅ Назад")
async def lifetime_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime); await m.answer("⏳ Вернулись к выбору срока жизни объявления:", reply_markup=kb_lifetime())

@router.message(AddEvent.upsell)
async def upsell_options(m: Message, state: FSMContext):
    txt = m.text
    if txt == "⬅ Назад":
        await state.clear(); return await m.answer("Главное меню:", reply_markup=kb_main())
    events = doc_events()["events"]; my_events = [e for e in events if e["author"] == m.from_user.id]
    if not my_events: await state.clear(); return await m.answer("❌ Не найдено созданных событий.", reply_markup=kb_main())
    current = my_events[-1]
    if txt == "🌍 Разместить без доп.опций": await state.clear(); return await m.answer("✅ Готово! Событие опубликовано.", reply_markup=kb_main())
    if txt == "⭐ ТОП на 7 дней":
        order_id = f"top_{current['id']}_{m.from_user.id}_{int(datetime.utcnow().timestamp())}"
        link, uuid = await cc_create_invoice(PRICES["top_week"], order_id, "PartyRadar TOP 7 days")
        if not link: return await m.answer("⚠ Не удалось получить ссылку на оплату.", reply_markup=kb_upsell())
        payments_add(order_id, m.from_user.id, uuid, PRICES["top_week"], "top", {"event_id": current["id"]})
        await state.update_data(_order_id_top=order_id, _uuid_top=uuid)
        return await m.answer(f"💳 Ссылка на оплату ТОП:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил» ещё раз.", reply_markup=kb_upsell())
    if txt == "📡 Push (30 км)":
        order_id = f"push_{current['id']}_{m.from_user.id}_{int(datetime.utcnow().timestamp())}"
        link, uuid = await cc_create_invoice(PRICES["push"], order_id, "PartyRadar PUSH 30km")
        if not link: return await m.answer("⚠ Не удалось получить ссылку на оплату.", reply_markup=kb_upsell())
        payments_add(order_id, m.from_user.id, uuid, PRICES["push"], "push", {"event_id": current["id"]})
        await state.update_data(_order_id_push=order_id, _uuid_push=uuid)
        return await m.answer(f"💳 Ссылка на оплату PUSH:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил» ещё раз.", reply_markup=kb_upsell())
    if txt == "✅ Я оплатил":
        st = await state.get_data()
        if st.get("_uuid_top") and await cc_is_paid(st["_uuid_top"]):
            payments_set_status(st["_order_id_top"], "paid")
            evs = doc_events()
            for e in evs["events"]:
                if e["id"] == payments_get(st["_order_id_top"])["payload"]["event_id"]:
                    e["is_top"] = True; e["top_expire"] = (datetime.utcnow() + timedelta(days=7)).isoformat()
            save_events(evs)
            await m.answer("✅ ТОП активирован на 7 дней!", reply_markup=kb_upsell())
        if st.get("_uuid_push") and await cc_is_paid(st["_uuid_push"]):
            payments_set_status(st["_order_id_push"], "paid")
            ev_id = payments_get(st["_order_id_push"])["payload"]["event_id"]
            ev = next((e for e in doc_events()["events"] if e["id"] == ev_id), None)
            if ev: await send_push_for_event(ev); await m.answer("✅ Push-рассылка отправлена активным пользователям в радиусе 30 км.", reply_markup=kb_upsell())
        return
    await m.answer("Выберите опцию из меню ниже:", reply_markup=kb_upsell())

async def publish_event(m: Message, state: FSMContext, data: dict, hours: int):
    media_files = data.get("media_files", [])
    if not media_files:
        logo_path = BASE_DIR / LOGO_FILENAME
        if logo_path.exists():
            media_files = [{"type": "photo", "file_id": str(logo_path), "is_local": True}]
    events_doc = doc_events(); new_id = next_id(events_doc["events"]); expires = datetime.utcnow() + timedelta(hours=hours)
    ev = {"id": new_id,"author": m.from_user.id,"title": data["title"],"description": data["description"],"category": data["category"],"datetime": data["datetime"],
          "lat": data["lat"],"lon": data["lon"],"media_files": media_files,"contact": data.get("contact"),"expire": expires.isoformat(),"notified": False,"is_top": False}
    events_doc["events"].append(ev); save_events(events_doc)
    await m.answer("✅ Событие успешно создано и опубликовано!", reply_markup=kb_main())

# ===================== SEARCH =====================
@router.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message):
    await m.answer(f"📍 Отправь геолокацию для поиска (скрепка → Геопозиция → точка на карте).\nПоиск в радиусе ~ {DEFAULT_RADIUS_KM} км.", reply_markup=kb_search_geo())

@router.message(F.location)
async def search_with_location(m: Message, state: FSMContext):
    users = doc_users(); users["users"][str(m.from_user.id)] = {"last_location": {"lat": m.location.latitude, "lon": m.location.longitude}, "last_seen": datetime.utcnow().isoformat()}; save_users(users)
    user_loc = (m.location.latitude, m.location.longitude); events = doc_events()["events"]; now = datetime.utcnow(); found = []
    for ev in events:
        try:
            if datetime.fromisoformat(ev["expire"]) <= now: continue
        except Exception:
            continue
        dist = km_distance(user_loc, (ev["lat"], ev["lon"]))
        if dist <= DEFAULT_RADIUS_KM: found.append((ev, dist))
    found.sort(key=lambda x: ((0 if x[0].get("is_top") else 1), x[1]))
    if not found:
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="➕ Создать событие")],[KeyboardButton(text="⬅ Назад")]], resize_keyboard=True)
        return await m.answer("😔 Событий рядом не найдено. Хочешь создать своё?", reply_markup=kb)
    for ev, dist in found:
        ev_text = format_event_card(ev) + f"\n📏 Расстояние: {dist:.1f} км"
        map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"; map_tg = f"https://t.me/share/url?url={map_g}"
        ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📍 Telegram", url=map_tg),InlineKeyboardButton(text="🌐 Google Maps", url=map_g)]])
        media = ev.get("media_files") or []
        for f in media:
            if f.get("is_local"): f["file_id"] = FSInputFile(f["file_id"])
        if len(media) > 1:
            from aiogram.types import InputMediaPhoto, InputMediaVideo
            group = []
            for i, f in enumerate(media):
                caption = ev_text if i == 0 else None
                if f["type"] == "photo": group.append(InputMediaPhoto(media=f["file_id"], caption=caption, parse_mode="HTML"))
                elif f["type"] == "video": group.append(InputMediaVideo(media=f["file_id"], caption=caption, parse_mode="HTML"))
            await bot.send_media_group(m.chat.id, group); await bot.send_message(m.chat.id, "🗺 <b>Локация:</b>", reply_markup=ikb, parse_mode="HTML")
        elif len(media) == 1:
            f = media[0]
            if f["type"] == "photo": await m.answer_photo(f["file_id"], caption=ev_text, reply_markup=ikb, parse_mode="HTML")
            elif f["type"] == "video": await m.answer_video(f["file_id"], caption=ev_text, reply_markup=ikb, parse_mode="HTML")
        else:
            await m.answer(ev_text, reply_markup=ikb, parse_mode="HTML")

# ===================== BANNERS =====================
@router.message(F.text == "🖼 Купить баннер")
async def banner_start(m: Message, state: FSMContext):
    await state.set_state(AddBanner.media)
    await m.answer("🖼 Загрузка баннера.\nПришлите <b>фото или видео</b> (текст можно указать в подписи, ссылку укажем далее).", reply_markup=kb_back())

@router.message(AddBanner.media, F.content_type == ContentType.PHOTO)
async def banner_media_photo(m: Message, state: FSMContext):
    file_id = m.photo[-1].file_id; text = (m.caption or "").strip()
    await state.update_data(b_media={"type": "photo", "file_id": file_id}, b_text=text)
    await state.set_state(AddBanner.url); await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@router.message(AddBanner.media, F.content_type == ContentType.VIDEO)
async def banner_media_video(m: Message, state: FSMContext):
    file_id = m.video.file_id; text = (m.caption or "").strip()
    await state.update_data(b_media={"type": "video", "file_id": file_id}, b_text=text)
    await state.set_state(AddBanner.url); await m.answer("🔗 Укажи ссылку (или напиши «Пропустить»).", reply_markup=kb_back())

@router.message(AddBanner.media)
async def banner_media_wrong(m: Message, state: FSMContext):
    if m.text == "⬅ Назад": await state.clear(); return await m.answer("Главное меню:", reply_markup=kb_main())
    await m.answer("⚠ Пришлите фото или видео баннера.", reply_markup=kb_back())

@router.message(AddBanner.url)
async def banner_url(m: Message, state: FSMContext):
    if m.text == "⬅ Назад": await state.set_state(AddBanner.media); return await m.answer("Пришлите фото/видео баннера.", reply_markup=kb_back())
    url = None if m.text.lower().strip() == "пропустить" else m.text.strip()
    await state.update_data(b_url=url); await state.set_state(AddBanner.geolocation)
    await m.answer("📍 Отправьте геолокацию региона показа (скрепка → Геопозиция).", reply_markup=kb_search_geo())

@router.message(AddBanner.geolocation, F.location)
async def banner_geo(m: Message, state: FSMContext):
    await state.update_data(b_lat=m.location.latitude, b_lon=m.location.longitude)
    await state.set_state(AddBanner.duration)
    await m.answer(f"⏳ Выберите срок показа баннера:\n• 7 дней — ${PRICES['banner_week']}\n• 30 дней — ${PRICES['banner_month']}", reply_markup=kb_banner_duration())

@router.message(AddBanner.geolocation)
async def banner_geo_wait(m: Message, state: FSMContext):
    if m.text == "⬅ Назад": await state.set_state(AddBanner.url); return await m.answer("🔗 Укажи ссылку (или «Пропустить»).", reply_markup=kb_back())
    await m.answer("⚠ Отправьте геолокацию баннера.", reply_markup=kb_search_geo())

@router.message(AddBanner.duration)
async def banner_duration(m: Message, state: FSMContext):
    if m.text == "⬅ Назад": await state.set_state(AddBanner.geolocation); return await m.answer("📍 Отправьте геолокацию региона баннера.", reply_markup=kb_search_geo())
    if m.text == "🗓️ Баннер на 7 дней": amount = PRICES["banner_week"]; days = 7
    elif m.text == "🗓️ Баннер на месяц": amount = PRICES["banner_month"]; days = 30
    else: return await m.answer("Выберите один из вариантов.", reply_markup=kb_banner_duration())
    data = await state.get_data(); b_lat = data.get("b_lat"); b_lon = data.get("b_lon")
    if b_lat is None or b_lon is None: await state.set_state(AddBanner.geolocation); return await m.answer("📍 Сначала отправьте геолокацию региона баннера.", reply_markup=kb_search_geo())
    order_id = f"banner_{m.from_user.id}_{int(datetime.utcnow().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link: return await m.answer("⚠ Не удалось получить ссылку. Проверь .env ключи и PUBLIC_URL.", reply_markup=kb_payment())
    payments_add(order_id, m.from_user.id, uuid, amount, "banner", {"days": days, "geo": [b_lat, b_lon], "data": data})
    await state.update_data(_order_id=order_id, _uuid=uuid, _days=days)
    await state.set_state(AddBanner.payment)
    await m.answer(f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@router.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    st = await state.get_data(); order_id = st.get("_order_id"); uuid = st.get("_uuid"); days = st.get("_days")
    if not (order_id and uuid and days): return await m.answer("❌ Счёт не найден. Получите ссылку ещё раз.", reply_markup=kb_payment())
    if not await cc_is_paid(uuid): return await m.answer("❌ Оплата не найдена. Подождите минуту и попробуйте снова.", reply_markup=kb_payment())
    payments_set_status(order_id, "paid")
    payload = payments_get(order_id)["payload"]; data = payload.get("data", {}); b_media = data.get("b_media"); b_text = (data.get("b_text") or "").strip(); b_url = data.get("b_url"); b_lat, b_lon = payload["geo"]
    banners = doc_banners(); new_id = next_id(banners["banners"]); expire = datetime.utcnow() + timedelta(days=days)
    banners["banners"].append({"id": new_id,"owner": m.from_user.id,"media_type": b_media["type"],"file_id": b_media["file_id"],"text": b_text,"url": b_url,"lat": b_lat,"lon": b_lon,"region": "geo","expire": expire.isoformat()})
    save_banners(banners); await state.clear(); await m.answer("✅ Баннер активирован и будет показан пользователям в выбранном регионе.", reply_markup=kb_main())

@router.message(AddBanner.payment, F.text == "🧾 Получить ссылку на оплату")
async def banner_link_again(m: Message, state: FSMContext):
    await m.answer("Ссылка уже была выдана выше. После оплаты нажмите «✅ Я оплатил».", reply_markup=kb_payment())

@router.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration); await m.answer("⏳ Выберите срок показа баннера:", reply_markup=kb_banner_duration())

# ===================== PUSH и демоны =====================
async def send_push_for_event(ev: dict):
    from datetime import datetime, timedelta
    udoc = doc_users(); now = datetime.utcnow(); c = 0
    for uid, u in udoc["users"].items():
        loc = u.get("last_location"); ts = u.get("last_seen")
        if not (loc and ts): continue
        try:
            if (now - datetime.fromisoformat(ts)) > timedelta(days=30): continue
        except Exception: continue
        if km_distance((ev["lat"], ev["lon"]), (loc["lat"], loc["lon"])) <= DEFAULT_RADIUS_KM:
            try: await send_event_media(int(uid), ev); c += 1
            except Exception: pass
    logging.info(f"Push sent to {c} users.")

async def push_daemon():
    while True:
        events = doc_events(); now = datetime.utcnow(); changed = False
        for ev in events["events"]:
            if ev.get("is_top") and ev.get("top_expire"):
                try:
                    if datetime.fromisoformat(ev["top_expire"]) <= now: ev["is_top"] = False; ev["top_expire"] = None; changed = True
                except Exception: pass
            if not ev.get("notified"):
                try: exp = datetime.fromisoformat(ev["expire"])
                except Exception: continue
                if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                    ev["notified"] = True; changed = True
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 +48 часов", callback_data=f"extend:{ev['id']}:48"),
                                                                InlineKeyboardButton(text="🗓 +1 неделя", callback_data=f"extend:{ev['id']}:168"),
                                                                InlineKeyboardButton(text="🏷 +2 недели", callback_data=f"extend:{ev['id']}:336")]])
                    try: await bot.send_message(ev["author"], f"⏳ Событие «{ev['title']}» скоро завершится. Хотите продлить?", reply_markup=kb)
                    except Exception: pass
        if changed: save_events(events)
        await asyncio.sleep(300)

async def cleanup_daemon():
    while True:
        now = datetime.utcnow()
        events = doc_events(); upd = []
        for ev in events["events"]:
            try:
                if datetime.fromisoformat(ev["expire"]) > now: upd.append(ev)
                else:
                    try: await bot.send_message(ev["author"], f"🗑 Событие «{ev['title']}» истекло и удалено.")
                    except Exception: pass
            except Exception: upd.append(ev)
        if len(upd) != len(events["events"]): events["events"] = upd; save_events(events)
        banners = doc_banners(); upd_b = []
        for b in banners["banners"]:
            try:
                if datetime.fromisoformat(b["expire"]) > now: upd_b.append(b)
            except Exception: upd_b.append(b)
        if len(upd_b) != len(banners["banners"]): banners["banners"] = upd_b; save_banners(banners)
        await asyncio.sleep(600)

# ===================== CALLBACK EXTEND =====================
@router.callback_query(F.data.startswith("extend:"))
async def extend_from_push(cq: CallbackQuery):
    try: _, ev_id, hours = cq.data.split(":"); ev_id = int(ev_id); hours = int(hours)
    except Exception: return await cq.answer("Ошибка параметров", show_alert=True)
    amount = {48: PRICES["extend_48h"], 168: PRICES["extend_week"], 336: PRICES["extend_2week"]}.get(hours)
    if not amount: return await cq.answer("Тариф не найден", show_alert=True)
    await cq.message.answer(f"Продление на {hours} ч — ${amount}.\nОткройте «➕ Создать событие» и выберите платный срок для публикации/продления.")
    await cq.answer()

# ===================== FALLBACK =====================
@router.message(F.text == "⬅ Назад")
async def global_back(m: Message):
    await m.answer("Главное меню:", reply_markup=kb_main())

@router.message()
async def fallback(m: Message):
    await m.answer("Я не понял команду. Используй кнопки ниже 👇", reply_markup=kb_main())

# ===================== WEBHOOK SERVER (Render) =====================
async def handle_payment_callback(request: web.Request) -> web.Response:
    try:
        if request.method == "POST":
            data = await request.json(); order_id = str(data.get("order_id") or ""); status = str(data.get("status") or "").lower()
        else:
            qs = request.rel_url.query; order_id = qs.get("order_id", ""); status = qs.get("status", "").lower()
        if not order_id: return web.Response(text="order_id missing")
        if status == "paid": payments_set_status(order_id, "paid")
        elif status == "failed": payments_set_status(order_id, "failed")
        return web.Response(text="ok")
    except Exception as e:
        logging.exception(f"/payment_callback error: {e}"); return web.Response(text="error", status=500)

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="PartyRadar OK")

async def telegram_handler(request: web.Request) -> web.Response:
    try:
        update = await request.json()
        await dp.feed_webhook_update(bot, update)
        return web.Response()
    except Exception as e:
        logging.exception(f"feed_webhook_update error: {e}")
        return web.Response(status=500)

def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/", handle_root), web.get("/payment_callback", handle_payment_callback), web.post("/payment_callback", handle_payment_callback)])
    app.router.add_post(f"/webhook/{BOT_TOKEN}", telegram_handler)
    return app

async def on_startup_webhook():
    if not PUBLIC_URL:
        logging.warning("PUBLIC_URL не задан — вебхук не будет установлен.")
        return
    webhook_url = f"{PUBLIC_URL}/webhook/{BOT_TOKEN}"
    try:
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logging.info(f"Webhook set: {webhook_url}")
    except Exception as e:
        logging.exception(f"set_webhook failed: {e}")

async def main():
    port = int(os.getenv("PORT", "0") or 0)
    if port:
        app = build_app()
        await on_startup_webhook()
        asyncio.create_task(push_daemon())
        asyncio.create_task(cleanup_daemon())
        runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "0.0.0.0", port); await site.start()
        logging.info(f"Serving webhook on 0.0.0.0:{port}")
        while True: await asyncio.sleep(3600)
    else:
        logging.info("Polling mode...")
        asyncio.create_task(push_daemon()); asyncio.create_task(cleanup_daemon())
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
