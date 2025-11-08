import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

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
import aiohttp
import traceback

# ===================== CONFIG =====================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv(\"CRYPTOCLOUD_API_KEY\", \"\").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv(\"CRYPTOCLOUD_SHOP_ID\", \"\").strip()
ADMIN_ID = int(os.getenv(\"ADMIN_ID\", \"0\"))

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())

EVENTS_FILE = \"events.json\"
BANNERS_FILE = \"banners.json\"
USERS_FILE = \"users.json\"  # для пушей/баннеров: последние геолокации пользователей
FAV_FILE = \"favorites.json\"

DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2

# ---------- БАННЕРНЫЕ СЛОТЫ ПО РЕГИОНАМ ----------
MAX_BANNERS_PER_REGION = 3
BANNER_REGION_RADIUS_KM = 30  # радиус объединения по региону (и проверки слотов)

# ---------- PRICES (в USD, счёт в CryptoCloud всегда в USD) ----------
PRICES = {
    \"extend_48h\": 1.0,
    \"extend_week\": 3.0,
    \"extend_2week\": 5.0,
    \"top_week\": 5.0,
    \"push\": 2.0,
    \"banner_week\": 10.0,
    \"banner_month\": 30.0,
}

# Соответствие часов — тариф
LIFETIME_OPTIONS = {
    \"🕐 24 часа (бесплатно)\": 24,
    \"📅 48 часов\": 48,
    \"🗓 1 неделя\": 168,
    \"🏷 2 недели\": 336
}
TARIFFS_USD = {
    48: PRICES[\"extend_48h\"],
    168: PRICES[\"extend_week\"],
    336: PRICES[\"extend_2week\"]
}

# ===================== STORAGE =====================
def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, \"r\", encoding=\"utf-8\") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    tmp = path + \".tmp\"
    with open(tmp, \"w\", encoding=\"utf-8\") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

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

def _load_favs() -> Dict[str, List[str]]:
    return _load_json(FAV_FILE, {})

def _save_favs(data: Dict[str, List[str]]):
    _save_json(FAV_FILE, data)

# ===================== CRYPTOCLOUD =====================
async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    \"\"\"
    Создаёт счёт в CryptoCloud. Возвращает (link, uuid) или (None, None) при ошибке.
    \"\"\"
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
        return None, None

    url = \"https://api.cryptocloud.plus/v2/invoice/create\"
    headers = {
        \"Authorization\": f\"Token {CRYPTOCLOUD_API_KEY}\",
        \"Content-Type\": \"application/json\"
    }
    payload = {
        \"shop_id\": CRYPTOCLOUD_SHOP_ID,
        \"amount\": float(amount_usd),
        \"currency\": \"USD\",
        \"order_id\": order_id,
        \"description\": description,
        \"locale\": \"ru\"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()
                link = data.get(\"result\", {}).get(\"link\")
                uuid = data.get(\"result\", {}).get(\"uuid\")
                return link, uuid
    except Exception as e:
        logging.exception(f\"CryptoCloud create error: {e}\")
    return None, None

async def cc_is_paid(invoice_uuid: str) -> bool:
    \"\"\"
    Проверяет статус счёта. Возвращает True, если оплачен.
    \"\"\"
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid):
        return False
    url = f\"https://api.cryptocloud.plus/v2/invoice/info/{invoice_uuid}\"
    headers = {\"Authorization\": f\"Token {CRYPTOCLOUD_API_KEY}\"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as resp:
                data = await resp.json()
                status = data.get(\"result\", {}).get(\"status\")
                return str(status).lower() == \"paid\"
    except Exception as e:
        logging.exception(f\"CryptoCloud check error: {e}\")
        return False

# ===================== FSM =====================
class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt = State()
    media = State()
    contact = State()
    lifetime = State()
    payment = State()      # подтверждение платного срока
    upsell = State()       # доп. опции (ТОП/Push/Пропустить)
    pay_option = State()   # оплата доп. опции (ТОП/Push)

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
            [KeyboardButton(text=\"➕ Создать событие\")],
            [KeyboardButton(text=\"📍 Найти события рядом\")],
            [KeyboardButton(text=\"💰 Тарифы и продвижение\")],
            [KeyboardButton(text=\"🖼 Купить баннер\"), KeyboardButton(text=\"⭐ Избранное")],
            [KeyboardButton(text=\"💬 Поддержка\")]
        ],
        resize_keyboard=True
    )

def kb_back():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=\"⬅ Назад\")]], resize_keyboard=True)

def kb_categories():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"🎉 Вечеринка\"), KeyboardButton(text=\"💬 Свидание\")],
            [KeyboardButton(text=\"🧠 Встреча по интересам\"), KeyboardButton(text=\"⚽ Активность/Спорт\")],
            [KeyboardButton(text=\"🧭 Другое")],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )

def kb_media_step():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"📍 Отправить геолокацию\", request_location=True)],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )

def kb_lifetime():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"🕐 24 часа (бесплатно)\"), KeyboardButton(text=\"📅 48 часов\")],
            [KeyboardButton(text=\"🗓 1 неделя\"), KeyboardButton(text=\"🏷 2 недели\")],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )

def kb_payment():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"💳 Получить ссылку на оплату\")],
            [KeyboardButton(text=\"✅ Я оплатил\")],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )

def kb_upsell():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"⭐ Разместить в ТОП (7 дней)\"), KeyboardButton(text=\"📡 Push-уведомление (30 км)\")],
            [KeyboardButton(text=\"🌍 Разместить бесплатно (без опций)\")],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )

def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"🗓 Баннер на 7 дней\"), KeyboardButton(text=\"📅 Баннер на месяц\")],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )

# ===================== HELPERS =====================
def format_event_card(ev: dict) -> str:
    dt = datetime.fromisoformat(ev[\"datetime\"])
    desc = f"\n📝 {ev['description']}" if ev.get(\"description\") else \""
    contact = f"\n☎ <b>Контакт:</b> {ev['contact']}" if ev.get(\"contact\") else \""
    top = \" 🔥<b>ТОП</b>\" if ev.get(\"is_top\") else \""
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
        InlineKeyboardButton(text=\"📍 Telegram\", url=map_tg),
        InlineKeyboardButton(text=\"🌐 Google Maps\", url=map_g)
    ]])
    media = ev.get(\"media_files\") or []
    for f in media:
        if f.get(\"is_local\"):
            f[\"file_id\"] = FSInputFile(f[\"file_id\"])
    if len(media) > 1:
        group = []
        for i, f in enumerate(media):
            caption = text if i == 0 else None
            if f[\"type\"] == \"photo\":
                group.append(InputMediaPhoto(media=f[\"file_id\"], caption=caption, parse_mode=\"HTML\"))
            elif f[\"type\"] == \"video\":
                group.append(InputMediaVideo(media=f[\"file_id\"], caption=caption, parse_mode=\"HTML\"))
        await bot.send_media_group(chat_id, group)
        await bot.send_message(chat_id, \"🗺 <b>Локация:</b>\", reply_markup=ikb, parse_mode=\"HTML\")
    elif len(media) == 1:
        f = media[0]
        if f[\"type\"] == \"photo\":
            await bot.send_photo(chat_id, f[\"file_id\"], caption=text, reply_markup=ikb, parse_mode=\"HTML\")
        elif f[\"type\"] == \"video\":
            await bot.send_video(chat_id, f[\"file_id\"], caption=text, reply_markup=ikb, parse_mode=\"HTML\")
    else:
        await bot.send_message(chat_id, text, reply_markup=ikb, parse_mode=\"HTML\")

def banners_in_region(center_lat: float, center_lon: float, banners: List[dict], now: datetime) -> List[dict]:
    active = []
    for b in banners:
        try:
            if datetime.fromisoformat(b[\"expire\"]) <= now:
                continue
        except Exception:
            continue
        if b.get(\"lat\") is None or b.get(\"lon\") is None:
            continue
        d = geodesic((center_lat, center_lon), (b[\"lat\"], b[\"lon\"])).km
        if d <= BANNER_REGION_RADIUS_KM:
            active.append(b)
    return active

def random_banner_for_user(user_data: dict, banners: List[dict]) -> Optional[dict]:
    now = datetime.now()
    loc = user_data.get(\"last_location\") if user_data else None
    if loc:
        region_banners = banners_in_region(loc[\"lat\"], loc[\"lon\"], banners, now)
        if region_banners:
            return random.choice(region_banners)
    global_candidates = []
    for b in banners:
        try:
            if datetime.fromisoformat(b[\"expire\"]) <= now:
                continue
        except Exception:
            continue
        if str(b.get(\"region\", \"\")).lower() == \"global\":
            global_candidates.append(b)
    if global_candidates:
        return random.choice(global_candidates)
    return None

# ===================== START =====================
@dp.message(Command(\"start\"))
async def start_cmd(m: Message):
    # показать баннер (если есть подходящий)
    users = _load_users()
    ukey = str(m.from_user.id)
    user_data = users.get(ukey, {})
    banners = _load_banners()
    banner = random_banner_for_user(user_data, banners)
    if banner:
        cap = (banner.get(\"text\") or \"Рекламный баннер\").strip()
        url = (banner.get(\"url\") or \"\").strip()
        cap_full = (cap + ("\n" + url if url else \"\")).strip()
        try:
            if banner.get(\"media_type\") == \"photo\":
                await m.answer_photo(banner[\"file_id\"], caption=cap_full)
            elif banner.get(\"media_type\") == \"video\":
                await m.answer_video(banner[\"file_id\"], caption=cap_full)
        except Exception:
            pass

    # логотип
    logo = None
    for ext in (\"png\", \"jpg\", \"jpeg\"):
        if os.path.exists(f\"logo.{ext}\"):
            logo = f\"logo.{ext}\"
            break
    welcome = (
        \"👋 Добро пожаловать в <b>PartyRadar</b>!\n\n\"
        \"🎉 Находи и создавай события: вечеринки, свидания, встречи по интересам, спорт и многое другое.\n\n\"
        \"📌 Объявления живут 24 часа бесплатно.\n\"
        \"💰 Можно выбрать платный срок, ТОП и Push при создании — всё на автомате.\"
    )
    if logo:
        try:
            await asyncio.sleep(1)
            await m.answer_photo(FSInputFile(logo), caption=welcome, reply_markup=kb_main(), parse_mode=\"HTML\")
        except Exception as e:
            logging.warning(f\"Logo send failed: {e}\")
            await m.answer(welcome, reply_markup=kb_main(), parse_mode=\"HTML\")
    else:
        await m.answer(welcome, reply_markup=kb_main(), parse_mode=\"HTML\")

# ===================== ТАРИФЫ =====================
@dp.message(F.text == \"💰 Тарифы и продвижение\")
async def show_tariffs(m: Message):
    text = (
        \"💰 <b>Тарифы PartyRadar</b>\n\n\"
        \"⏳ Сроки показа объявления:\n\"
        f\"• 24 часа — бесплатно\n\"
        f\"• 48 часов — ${PRICES['extend_48h']}\n\"
        f\"• 1 неделя — ${PRICES['extend_week']}\n\"
        f\"• 2 недели — ${PRICES['extend_2week']}\n\n\"
        f\"⭐ ТОП 7 дней — ${PRICES['top_week']}\n\"
        f\"📡 Push (30 км) — ${PRICES['push']}\n\"
        f\"🖼 Баннер 7 дней — ${PRICES['banner_week']} / месяц — ${PRICES['banner_month']}\n\n\"
        \"Оплата: счёт в USD через CryptoCloud → оплата TON/USDT, авто-конверсия.\"
    )
    await m.answer(text, parse_mode=\"HTML\")

# ===================== СОЗДАНИЕ СОБЫТИЯ =====================
@dp.message(F.text == \"➕ Создать событие\")
async def create_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer(\"📝 Введи <b>название</b> события:\", reply_markup=kb_back(), parse_mode=\"HTML\")

@dp.message(AddEvent.title)
async def step_title(m: Message, state: FSMContext):
    if m.text == \"⬅ Назад\":
        await state.clear()
        return await m.answer(\"Главное меню:\", reply_markup=kb_main())
    await state.update_data(title=m.text.strip())
    await state.set_state(AddEvent.description)
    await m.answer(\"🧾 Введи <b>описание</b> события:\", reply_markup=kb_back(), parse_mode=\"HTML\")

@dp.message(AddEvent.description)
async def step_description(m: Message, state: FSMContext):
    if m.text == \"⬅ Назад\":
        await state.set_state(AddEvent.title)
        return await m.answer(\"📝 Введи название:\", reply_markup=kb_back())
    await state.update_data(description=m.text.strip())
    await state.set_state(AddEvent.category)
    await m.answer(\"🧭 Выбери категорию:\", reply_markup=kb_categories())

@dp.message(AddEvent.category)
async def step_category(m: Message, state: FSMContext):
    if m.text == \"⬅ Назад\":
        await state.set_state(AddEvent.description)
        return await m.answer(\"🧾 Введи описание:\", reply_markup=kb_back())
    await state.update_data(category=m.text.strip())
    await state.set_state(AddEvent.dt)
    await m.answer(\"📆 Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\", reply_markup=kb_back())

@dp.message(AddEvent.dt)
async def step_datetime(m: Message, state: FSMContext):
    if m.text == \"⬅ Назад\":
        await state.set_state(AddEvent.category)
        return await m.answer(\"🧭 Выбери категорию:\", reply_markup=kb_categories())
    try:
        dt = datetime.strptime(m.text.strip(), \"%d.%m.%Y %H:%M\")
        if dt <= datetime.now():
            return await m.answer(\"⚠ Нельзя указывать прошедшее время.\", reply_markup=kb_back())
    except ValueError:
        return await m.answer(\"⚠ Неверный формат. Пример: 25.10.2025 19:30\", reply_markup=kb_back())
    await state.update_data(datetime=dt.isoformat(), media_files=[])
    await state.set_state(AddEvent.media)
    await m.answer(
        \"📎 Прикрепи до 3 файлов (фото/видео) или сразу отправь геолокацию.\n\"
        \"📍 Скрепка → Геопозиция → точка на карте.\n\"
        \"⚠ Аудио и кружки не поддерживаются.\",
        reply_markup=kb_media_step()
    )

@dp.message(AddEvent.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def step_media(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get(\"media_files\", [])
    if len(files) >= 3:
        return await m.answer(\"⚠ Уже 3 файла. Отправь геолокацию.\", reply_markup=kb_media_step())
    if m.photo:
        files.append({\"type\": \"photo\", \"file_id\": m.photo[-1].file_id})
    elif m.video:
        files.append({\"type\": \"video\", \"file_id\": m.video.file_id})
    await state.update_data(media_files=files)
    left = 3 - len(files)
    await m.answer(f\"✅ Файл добавлен ({len(files)}/3). \"
                   + (\"Можно добавить ещё или \" if left else \"\")
                   + \"отправь геолокацию для следующего шага.\", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.content_type.in_({ContentType.VOICE, ContentType.AUDIO}))
async def media_not_supported(m: Message, state: FSMContext):
    await m.answer(\"⚠ Аудио и кружки не поддерживаются. Прикрепи фото/видео.\", reply_markup=kb_media_step())

@dp.message(AddEvent.media, F.text == \"⬅ Назад\")
async def media_back(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get(\"media_files\", [])
    if files:
        files.pop()
        await state.update_data(media_files=files)
        await m.answer(f\"🗑 Удалён последний файл ({len(files)}/3).\", reply_markup=kb_media_step())
    else:
        await state.set_state(AddEvent.dt)
        await m.answer(\"📆 Вернулись к дате/времени. Введи ДД.ММ.ГГГГ ЧЧ:ММ\", reply_markup=kb_back())

@dp.message(AddEvent.media, F.location)
async def step_media_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    # сохраним локацию и в базе пользователей (для пушей/баннеров)
    users = _load_users()
    users[str(m.from_user.id)] = {
        \"last_location\": {\"lat\": m.location.latitude, \"lon\": m.location.longitude},
        \"last_seen\": datetime.now().isoformat()
    }
    _save_users(users)

    await state.set_state(AddEvent.contact)
    await m.answer(\"☎ Укажи контакт (@username, телефон или ссылка). Или напиши «Пропустить».\", reply_markup=kb_back())

@dp.message(AddEvent.contact)
async def step_contact(m: Message, state: FSMContext):
    if m.text == \"⬅ Назад\":
        await state.set_state(AddEvent.media)
        return await m.answer(\"Вернулись к медиафайлам:\", reply_markup=kb_media_step())
    if m.text.lower().strip() != \"пропустить\":
        await state.update_data(contact=m.text.strip())
    await state.set_state(AddEvent.lifetime)
    await m.answer(\"⏳ Выбери срок жизни объявления:\", reply_markup=kb_lifetime())

# ---- Выбор срока + платёж за платные сроки ----
@dp.message(AddEvent.lifetime)
async def step_lifetime(m: Message, state: FSMContext):
    if m.text == \"⬅ Назад\":
        await state.set_state(AddEvent.contact)
        return await m.answer(\"☎ Укажи контакт или напиши «Пропустить».\", reply_markup=kb_back())

    if m.text not in LIFETIME_OPTIONS:
        return await m.answer(\"Выбери вариант из списка:\", reply_markup=kb_lifetime())

    hours = LIFETIME_OPTIONS[m.text]

    # Бесплатно — сразу публикуем и переходим к апселлу опций (ТОП/Push)
    if hours == 24:
        data = await state.get_data()
        await publish_event(m, state, data, hours)
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            \"💡 Дополнительные опции монетизации:\n\n\"
            \"⭐ <b>ТОП на 7 дней</b> — ваше событие будет показываться первым в выдаче региона.\n\"
            \"📡 <b>Push (30 км)</b> — мгновенная рассылка всем активным пользователям в радиусе точки события.\n\n\"
            \"Выберите опцию или разместите бесплатно.\",
            parse_mode=\"HTML\",
            reply_markup=kb_upsell()
        )

    # Платно — показать описание и попросить оплату
    amount = TARIFFS_USD[hours]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None)
    description = (
        f\"⏳ <b>Платный тариф</b>\n\n\"
        f\"Вы выбрали: <b>{m.text}</b>\n\"
        f\"Стоимость: <b>${amount}</b>\n\n\"
        \"Что вы получаете:\n\"
        \"• дольше показ в выдаче → больше просмотров;\n\"
        \"• событие не исчезнет через 24 часа;\n\"
        \"• больше шансов собрать гостей.\n\n\"
        \"Нажмите «💳 Получить ссылку на оплату» для оплаты через CryptoCloud.\n\"
        \"Счёт в USD, оплата TON/USDT (авто-конверсия).\"
    )
    await state.set_state(AddEvent.payment)
    await m.answer(description, parse_mode=\"HTML\", reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == \"💳 Получить ссылку на оплату\")
async def lifetime_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get(\"paid_lifetime\")
    if not hours:
        return await m.answer(\"❌ Нет активного платного тарифа.\", reply_markup=kb_main())
    amount = TARIFFS_USD[hours]
    order_id = f\"lifetime_{hours}_{m.from_user.id}_{int(datetime.now().timestamp())}\"
    link, uuid = await cc_create_invoice(amount, order_id, f\"PartyRadar: {hours}h lifetime\")
    if not link:
        return await m.answer(\"⚠ Не удалось получить ссылку на оплату. Проверь .env ключи.\", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f\"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».\", reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == \"✅ Я оплатил\")
async def lifetime_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get(\"_pay_uuid\")
    hours = data.get(\"paid_lifetime\")
    if not (uuid and hours):
        return await m.answer(\"❌ Счёт не найден. Получите ссылку ещё раз.\", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer(\"❌ Оплата не найдена. Подождите минуту и попробуйте снова.\", reply_markup=kb_payment())
    # публикуем событие
    await publish_event(m, state, data, hours)
    # апселл опций
    await state.set_state(AddEvent.upsell)
    await m.answer(
        \"✅ Событие опубликовано!\n\n\"
        \"💡 Дополнительные опции монетизации:\n\"
        f\"• ⭐ ТОП 7 дней — ${PRICES['top_week']}\n\"
        f\"• 📡 Push (30 км) — ${PRICES['push']}\n\n\"
        \"Выберите опцию или разместите бесплатно.\",
        reply_markup=kb_upsell()
    )

@dp.message(AddEvent.payment, F.text == \"⬅ Назад\")
async def lifetime_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    await m.answer(\"⏳ Вернулись к выбору срока жизни объявления:\", reply_markup=kb_lifetime())

# ---- Дополнительные опции (ТОП / PUSH) ----
@dp.message(AddEvent.upsell)
async def upsell_options(m: Message, state: FSMContext):
    text = m.text
    if text == \"⬅ Назад\":
        await state.clear()
        return await m.answer(\"Главное меню:\", reply_markup=kb_main())

    data = await state.get_data()
    events = _load_events()
    my_events = [e for e in events if e[\"author\"] == m.from_user.id]
    if not my_events:
        await state.clear()
        return await m.answer(\"❌ Не найдено созданных событий.\", reply_markup=kb_main())
    current_event = my_events[-1]  # последнее созданное

    if text == \"🌍 Разместить бесплатно (без опций)\":
        await state.clear()
        return await m.answer(\"✅ Готово! Событие опубликовано.\", reply_markup=kb_main())

    if text == \"⭐ Разместить в ТОП (7 дней)\":
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_type=\"top\", opt_event_id=current_event[\"id\"], _pay_uuid=None)
        return await m.answer(
            f\"⭐ <b>ТОП на 7 дней</b> — ваше событие будет показываться первым в выдаче региона.\n\"
            f\"Стоимость: ${PRICES['top_week']}\n\n\"
            \"Нажмите «💳 Получить ссылку на оплату».\",
            parse_mode=\"HTML\",
            reply_markup=kb_payment()
        )

    if text == \"📡 Push-уведомление (30 км)\":
        await state.set_state(AddEvent.pay_option)
        await state.update_data(opt_type=\"push\", opt_event_id=current_event[\"id\"], _pay_uuid=None)
        return await m.answer(
            f\"📡 <b>Push-уведомление</b> — сообщение получат активные пользователи в радиусе 30 км от точки события.\n\"
            f\"Стоимость: ${PRICES['push']}\n\n\"
            \"Нажмите «💳 Получить ссылку на оплату».\",
            parse_mode=\"HTML\",
            reply_markup=kb_payment()
        )

    return await m.answer(\"Выберите опцию из меню ниже:\", reply_markup=kb_upsell())

@dp.message(AddEvent.pay_option, F.text == \"💳 Получить ссылку на оплату\")
async def opt_get_link(m: Message, state: FSMContext):
    data = await state.get_data()
    opt = data.get(\"opt_type\")
    ev_id = data.get(\"opt_event_id\")
    if not (opt and ev_id):
        return await m.answer(\"❌ Опция не выбрана.\", reply_markup=kb_upsell())

    amount = PRICES[\"top_week\"] if opt == \"top\" else PRICES[\"push\"]
    order_id = f\"{opt}_{ev_id}_{m.from_user.id}_{int(datetime.now().timestamp())}\"
    link, uuid = await cc_create_invoice(amount, order_id, f\"PartyRadar {opt}\")
    if not link:
        return await m.answer(\"⚠ Не удалось получить ссылку на оплату. Проверь .env ключи.\", reply_markup=kb_payment())
    await state.update_data(_pay_uuid=uuid)
    await m.answer(f\"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажмите «✅ Я оплатил».\", reply_markup=kb_payment())

@dp.message(AddEvent.pay_option, F.text == \"✅ Я оплатил\")
async def opt_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get(\"_pay_uuid\")
    opt = data.get(\"opt_type\")
    ev_id = data.get(\"opt_event_id\")
    if not (uuid and opt and ev_id):
        return await m.answer(\"❌ Счёт не найден. Получите ссылку ещё раз.\", reply_markup=kb_payment())
    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer(\"❌ Оплата не найдена. Подождите минуту и попробуйте снова.\", reply_markup=kb_payment())

    # Применяем опцию
    events = _load_events()
    target = next((e for e in events if e[\"id\"] == ev_id), None)
    if not target:
        await state.clear()
        return await m.answer(\"❌ Событие не найдено.\", reply_markup=kb_main())

    if opt == \"top\":
        target[\"is_top\"] = True
        target[\"top_expire\"] = (datetime.now() + timedelta(days=7)).isoformat()
        _save_events(events)
        await m.answer(\"✅ ТОП активирован на 7 дней!\", reply_markup=kb_upsell())

    elif opt == \"push\":
        await send_push_for_event(target)
        await m.answer(\"✅ Push-рассылка отправлена активным пользователям в радиусе 30 км.\", reply_markup=kb_upsell())

@dp.message(AddEvent.pay_option, F.text == \"⬅ Назад\")
async def opt_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer(\"Выберите дополнительную опцию:\", reply_markup=kb_upsell())

# ---------- ПУБЛИКАЦИЯ СОБЫТИЯ ----------
async def publish_event(m: Message, state: FSMContext, data: dict, hours: int):
    media_files = data.get(\"media_files\", [])
    if not media_files:
        for ext in (\"png\", \"jpg\", \"jpeg\"):
            if os.path.exists(f\"logo.{ext}\"):
                media_files = [{\"type\": \"photo\", \"file_id\": f\"logo.{ext}\", \"is_local\": True}]
                break
    events = _load_events()
    expires = datetime.now() + timedelta(hours=hours)
    new_id = (events[-1][\"id\"] + 1) if events else 1
    ev = {
        \"id\": new_id,
        \"author\": m.from_user.id,
        \"title\": data[\"title\"],
        \"description\": data[\"description\"],
        \"category\": data[\"category\"],
        \"datetime\": data[\"datetime\"],
        \"lat\": data[\"lat\"],
        \"lon\": data[\"lon\"],
        \"media_files\": media_files,
        \"contact\": data.get(\"contact\"),
        \"expire\": expires.isoformat(),
        \"notified\": False,
        \"is_top\": False
    }
    events.append(ev)
    _save_events(events)
    await m.answer(\"✅ Событие успешно создано и опубликовано!\", reply_markup=kb_main())

# ===================== ПОИСК СОБЫТИЙ =====================
@dp.message(F.text == \"📍 Найти события рядом\")
async def search_start(m: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=\"📍 Отправить геолокацию\", request_location=True)],
            [KeyboardButton(text=\"⬅ Назад\")]
        ],
        resize_keyboard=True
    )
    await m.answer(
        f\"📍 Отправь геолокацию для поиска (скрепка → Геопозиция → точка на карте).\n\"
        f\"Поиск в радиусе ~ {DEFAULT_RADIUS_KM} км.\",
        reply_markup=kb
    )

@dp.message(F.location)
async def search_with_location(m: Message):
    # Сохраним локацию пользователя (для пушей и баннеров)
    users = _load_users()
    users[str(m.from_user.id)] = {
        \"last_location\": {\"lat\": m.location.latitude, \"lon\": m.location.longitude},
        \"last_seen\": datetime.now().isoformat()
    }
    _save_users(users)

    user_loc = (m.location.latitude, m.location.longitude)
    events = _load_events()
    now = datetime.now()
    found = []
    for ev in events:
        try:
            if datetime.fromisoformat(ev[\"expire\"]) <= now:
                continue
        except Exception:
            continue
        dist = geodesic(user_loc, (ev[\"lat\"], ev[\"lon\"])).km
        if dist <= DEFAULT_RADIUS_KM:
            found.append((ev, dist))

    # ТОП сначала
    found.sort(key=lambda x: ((0 if x[0].get(\"is_top\") else 1), x[1]))

    if not found:
        return await m.answer(\"😔 Событий рядом не найдено. Нажми «➕ Создать событие», чтобы добавить своё.\", reply_markup=kb_main())

    for ev, dist in found:
        text = format_event_card(ev) + f"\n📏 Расстояние: {dist:.1f} км"
        map_g = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        map_tg = f"https://t.me/share/url?url={map_g}"
        ikb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=\"📍 Telegram\", url=map_tg),
            InlineKeyboardButton(text=\"🌐 Google Maps\", url=map_g)
        ]])
        media = ev.get(\"media_files\") or []
        for f in media:
            if f.get(\"is_local\"):
                f[\"file_id\"] = FSInputFile(f[\"file_id\"])
        if len(media) > 1:
            group = []
            for i, f in enumerate(media):
                caption = text if i == 0 else None
                if f[\"type\"] == \"photo\":
                    group.append(InputMediaPhoto(media=f[\"file_id\"], caption=caption, parse_mode=\"HTML\"))
                elif f[\"type\"] == \"video\":
                    group.append(InputMediaVideo(media=f[\"file_id\"], caption=caption, parse_mode=\"HTML\"))
            await bot.send_media_group(m.chat.id, group)
            await bot.send_message(m.chat.id, \"🗺 <b>Локация:</b>\", reply_markup=ikb, parse_mode=\"HTML\")
        elif len(media) == 1:
            f = media[0]
            if f[\"type\"] == \"photo\":
                await m.answer_photo(f[\"file_id\"], caption=text, reply_markup=ikb, parse_mode=\"HTML\")
            elif f[\"type\"] == \"video\":
                await m.answer_video(f[\"file_id\"], caption=text, reply_markup=ikb, parse_mode=\"HTML\")
        else:
            await m.answer(text, reply_markup=ikb, parse_mode=\"HTML\")

# ===================== ИЗБРАННОЕ =====================
@dp.message(F.text == \"⭐ Избранное\")
async def fav_list(m: Message):
    favs = _load_favs()
    lst = favs.get(str(m.from_user.id)) or []
    if not lst:
        await m.answer(\"⭐ В избранном пока пусто.\")
        return
    events = _load_events()
    id2ev = {str(e.get(\"id\")): e for e in events}
    real = [id2ev[i] for i in lst if i in id2ev]
    if not real:
        await m.answer(\"⭐ Актуальных событий в избранном нет.\")
        return
    for ev in real:
        await send_event_media(m.chat.id, ev)
        await asyncio.sleep(0.2)

@dp.callback_query(F.data.startswith(\"fav:\"))
async def fav_toggle(cq: CallbackQuery):
    parts = cq.data.split(\":\")
    action, ev_id = parts[1], parts[2]
    favs = _load_favs()
    uid = str(cq.from_user.id)
    favs.setdefault(uid, [])
    if action == \"add\":
        if ev_id not in favs[uid]:
            favs[uid].append(ev_id)
        await cq.answer(\"Добавлено в избранное\")
    else:
        favs[uid] = [x for x in favs[uid] if x != ev_id]
        await cq.answer(\"Удалено из избранного\")
    _save_favs(favs)

@dp.callback_query(F.data.startswith(\"ev:map:\"))
async def ev_map(cq: CallbackQuery):
    ev_id = cq.data.split(\":\")[2]
    events = _load_events()
    ev = next((e for e in events if str(e.get(\"id\")) == ev_id), None)
    if not ev:
        await cq.answer(\"Событие не найдено\", show_alert=True)
        return
    url = f\"https://www.google.com/maps?q={ev['lat']},{ev['lon']}\"
    await cq.message.answer(f\"🌐 {url}\")
    await cq.answer()

# ===================== PUSH-рассылка для события =====================
async def send_push_for_event(ev: dict):
    users = _load_users()
    now = datetime.now()
    count = 0
    for uid, u in users.items():
        loc = u.get(\"last_location\")
        ts = u.get(\"last_seen\")
        if not (loc and ts):
            continue
        try:
            if (now - datetime.fromisoformat(ts)) > timedelta(days=30):
                continue
        except Exception:
            continue
        d = geodesic((ev[\"lat\"], ev[\"lon\"]), (loc[\"lat\"], loc[\"lon\"])).km
        if d <= DEFAULT_RADIUS_KM:
            try:
                await send_event_media(int(uid), ev)
                count += 1
            except Exception:
                pass
    logging.info(f\"Push sent to {count} users.\")

# ===================== PUSH-уведомление о скором окончании =====================
async def push_daemon():
    while True:
        events = _load_events()
        now = datetime.now()
        changed = False
        for ev in events:
            # снять просроченный ТОП
            if ev.get(\"is_top\") and ev.get(\"top_expire\"):
                try:
                    if datetime.fromisoformat(ev[\"top_expire\"]) <= now:
                        ev[\"is_top\"] = False
                        ev[\"top_expire\"] = None
                        changed = True
                except Exception:
                    pass
            # уведомление за 2 часа
            if ev.get(\"notified\"):
                continue
            try:
                exp = datetime.fromisoformat(ev[\"expire\"])
            except Exception:
                continue
            if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                ev[\"notified\"] = True
                changed = True
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=\"📅 +48 часов\", callback_data=f\"extend:{ev['id']}:48\")],
                        [InlineKeyboardButton(text=\"🗓 +1 неделя\", callback_data=f\"extend:{ev['id']}:168\")],
                        [InlineKeyboardButton(text=\"🏷 +2 недели\", callback_data=f\"extend:{ev['id']}:336\")]
                    ]
                )
                try:
                    await bot.send_message(
                        ev[\"author\"],
                        f\"⏳ Событие «{ev['title']}» скоро завершится. Хотите продлить?\",
                        reply_markup=kb
                    )
                except Exception:
                    pass
        if changed:
            _save_events(events)
        await asyncio.sleep(300)

# ===================== АВТО-ОЧИСТКА СОБЫТИЙ И БАННЕРОВ =====================
async def cleanup_daemon():
    while True:
        now = datetime.now()
        # events
        events = _load_events()
        updated = []
        for ev in events:
            try:
                if datetime.fromisoformat(ev[\"expire\"]) > now:
                    updated.append(ev)
                else:
                    try:
                        await bot.send_message(ev[\"author\"], f\"🗑 Событие «{ev['title']}» истекло и удалено.\")
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
                if datetime.fromisoformat(b[\"expire\"]) > now:
                    banners_updated.append(b)
            except Exception:
                banners_updated.append(b)
        if len(banners_updated) != len(banners):
            _save_banners(banners_updated)

        await asyncio.sleep(600)

# ===================== CALLBACK продления из пуша =====================
@dp.callback_query(F.data.startswith(\"extend:\"))
async def extend_from_push(cq: CallbackQuery, state: FSMContext):
    _, ev_id, hours = cq.data.split(\":\")
    ev_id = int(ev_id); hours = int(hours)
    amount = TARIFFS_USD.get(hours)
    if not amount:
        return await cq.answer(\"Тариф не найден\", show_alert=True)
    # Просто информируем, где оформить продление (в текущей версии — заново публиковать с платным сроком)
    await cq.message.answer(
        f\"Продление на {hours} ч — ${amount}.\n\"
        \"Откройте «➕ Создать событие» и выберите платный срок для публикации/продления.\",
    )
    await cq.answer()

# ===================== БАННЕРЫ =====================
@dp.message(F.text == \"🖼 Купить баннер\")
async def banner_start(m: Message, state: FSMContext):
    await m.answer(
        \"🖼 Баннеры показываются в /start пользователям региона.\n\"
        \"Сейчас подключаем партнёра по платежам. После модерации добавим оплату и форму загрузки.\",
        reply_markup=kb_main()
    )

# ===================== SUPPORT =====================
@dp.message(F.text == \"💬 Поддержка\")
async def support(m: Message):
    await m.answer(\"📩 Поддержка: @ТВОЙ_ЮЗЕР\", reply_markup=kb_main())

# ===================== GLOBAL BACK =====================
@dp.message(F.text == \"⬅ Назад\")
async def global_back(m: Message):
    await m.answer(\"Главное меню:\", reply_markup=kb_main())

@dp.message()
async def fallback(m: Message):
    await m.answer(\"Я не понял команду. Используй кнопки ниже 👇\", reply_markup=kb_main())

# ===================== RUN =====================
async def main():
    logging.info(\"✅ PartyRadar запущен…\")
    asyncio.create_task(push_daemon())     # напоминания и истечение ТОПа
    asyncio.create_task(cleanup_daemon())  # авто-очистка событий и баннеров
    await dp.start_polling(bot)

if __name__ == \"__main__\":
    asyncio.run(main())
