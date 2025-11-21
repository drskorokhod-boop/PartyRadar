import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Location,
    ContentType as CT,
    InputMediaPhoto,
    InputMediaVideo,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.utils.markdown import hlink
from aiogram.client.default import DefaultBotProperties

API_TOKEN = os.getenv("BOT_TOKEN")

if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

logging.basicConfig(level=logging.INFO)

bot = Bot(API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

USERS_FILE = "users.json"
EVENTS_FILE = "events.json"
BANNERS_FILE = "banners.json"
PAYMENTS_FILE = "payments.json"

DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2
MAX_ACTIVE_BANNERS = 3
ANYPAY_VERIFICATION_TEXT = "0298a93952ce16ab5114a95d874d"

TARIFFS_USD = {
    24: 1.0,
    72: 1.5,
    168: 3.0,
}

TOP_PRICES = {
    1: 1.0,
    3: 2.0,
    7: 4.0,
}

PUSH_PRICE = 2.5

BANNER_DURATIONS = {
    "1 день": (1, 3.0),
    "3 дня": (3, 7.0),
    "7 дней": (7, 15.0),
}

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "")
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "")

MOD_CHAT_ID = os.getenv("MOD_CHAT_ID")
ADMINS = set()
if MOD_CHAT_ID:
    try:
        ADMINS.add(int(MOD_CHAT_ID))
    except ValueError:
        pass

FSInputFile("imgonline-com-ua-Resize-poVtNXt7aue6.png")


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.exception(f"Error loading {path}: {e}")
        return default


def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"Error saving {path}: {e}")


def _load_users() -> List[Dict[str, Any]]:
    return load_json(USERS_FILE, [])


def _save_users(users: List[Dict[str, Any]]):
    save_json(USERS_FILE, users)


def _load_events() -> List[Dict[str, Any]]:
    return load_json(EVENTS_FILE, [])


def _save_events(events: List[Dict[str, Any]]):
    save_json(EVENTS_FILE, events)


def _load_banners() -> List[Dict[str, Any]]:
    return load_json(BANNERS_FILE, [])


def _save_banners(banners: List[Dict[str, Any]]):
    save_json(BANNERS_FILE, banners)


def _load_payments() -> List[Dict[str, Any]]:
    return load_json(PAYMENTS_FILE, [])


def _save_payments(payments: List[Dict[str, Any]]):
    save_json(PAYMENTS_FILE, payments)


def sanitize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip()


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _safe_dt(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


class AddEvent(StatesGroup):
    type = State()
    description = State()
    media = State()
    contact = State()
    lifetime = State()
    confirm = State()
    upsell = State()
    pay_option = State()
    upsell_more = State()


class AddBanner(StatesGroup):
    duration = State()
    payment = State()


class PushBroadcast(StatesGroup):
    radius = State()
    text = State()
    confirm = State()


def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Найти события рядом")],
            [KeyboardButton(text="➕ Создать событие")],
            [KeyboardButton(text="💬 Чат в радиусе 10 км")],
            [KeyboardButton(text="ℹ️ Помощь")],
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
        keyboard=[
            [KeyboardButton(text="📸 Фото/видео"), KeyboardButton(text="📍 Геолокация")],
            [KeyboardButton(text="Пропустить")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_lifetime():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="24 часа (бесплатно)")],
            [KeyboardButton(text="3 дня (платно)"), KeyboardButton(text="7 дней (платно)")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_confirm():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Опубликовать")],
            [KeyboardButton(text="⬅ Назад")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True
    )


def kb_upsell():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Продвижение ТОП")],
            [KeyboardButton(text="📣 Push-рассылка (30 км)")],
            [KeyboardButton(text="🖼 Баннер (премиум)")],
            [KeyboardButton(text="🌍 Оставить без доп.опций")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_upsell_more():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Ещё ТОП")],
            [KeyboardButton(text="📣 Ещё Push")],
            [KeyboardButton(text="🖼 Ещё баннер")],
            [KeyboardButton(text="⬅ Завершить")],
        ],
        resize_keyboard=True
    )


def kb_top_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ 1 день"), KeyboardButton(text="⭐ 3 дня")],
            [KeyboardButton(text="⭐ 7 дней")],
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


def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1 день"), KeyboardButton(text="3 дня")],
            [KeyboardButton(text="7 дней")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_help():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Правила / Безопасность")],
            [KeyboardButton(text="Как работает PartyRadar")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_radius():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="10 км"), KeyboardButton(text="30 км")],
            [KeyboardButton(text="50 км")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_skip():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Пропустить"), KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


async def send_logo_then_welcome(m: Message):
    try:
    await m.answer_photo(FSInputFile("imgonline-com-ua-Resize-poVtNXt7aue6.png")
except Exception:
    pass
        
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


async def send_banner_for_user(m: Message):
    """Показывает один актуальный баннер рядом с пользователем (если есть)."""
    users = _load_users()
    me = next((u for u in users if u.get("user_id") == m.from_user.id), None)
    if not me or me.get("lat") is None or me.get("lon") is None:
        # Не знаем локацию пользователя — баннер по радиусу показать не можем
        return

    lat = me["lat"]
    lon = me["lon"]

    banners = _load_banners()
    now = datetime.now()
    active = []
    for b in banners:
        status = b.get("status", "active")
        exp = _safe_dt(b.get("expire"))
        if status == "expired":
            continue
        if exp and exp <= now:
            continue
        b_lat = b.get("lat")
        b_lon = b.get("lon")
        if b_lat is None or b_lon is None:
            continue
        dist = haversine(lat, lon, b_lat, b_lon)
        if dist <= DEFAULT_RADIUS_KM:
            b = dict(b)
            b["distance"] = dist
            active.append(b)

    if not active:
        return

    # Берём самый близкий баннер
    active.sort(key=lambda x: x.get("distance", 999999))
    b = active[0]

    text_parts = ["🖼 <b>Интересный баннер рядом</b>"]
    if b.get("text"):
        text_parts.append(b["text"])
    if b.get("link"):
        text_parts.append(f"🔗 {b['link']}")
    caption = "\n\n".join(text_parts)

    media_type = b.get("media_type")
    file_id = b.get("file_id")

    try:
        if media_type == "photo" and file_id:
            await m.answer_photo(file_id, caption=caption)
        elif media_type == "video" and file_id:
            await m.answer_video(file_id, caption=caption)
        else:
            await m.answer(caption)
    except Exception as e:
        logging.exception(f"Ошибка отправки баннера пользователю {m.from_user.id}: {e}")


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
    await send_banner_for_user(m)


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "❓ <b>Как пользоваться PartyRadar</b>\n\n"
        "1. Отправь геолокацию — увидишь события рядом.\n"
        "2. Создай своё событие через «➕ Создать событие».\n"
        "3. Добавь медиа, описание и контакт.\n"
        "4. При желании подключи ТОП / Push / Баннер.\n\n"
        "Все объявления автоматически скрываются по истечении срока.",
        reply_markup=kb_main()
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_menu(m: Message):
    await m.answer(
        "Что именно подсказать?",
        reply_markup=kb_help()
    )


@dp.message(F.text == "Правила / Безопасность")
async def help_rules(m: Message):
    await m.answer(
        "⚠️ <b>Безопасность и правила</b>\n\n"
        "• Встречайся только в людных местах.\n"
        "• Не передавай предоплату незнакомым людям.\n"
        "• Сообщай о подозрительных объявлениях.\n"
        "• Запрещены наркотики, оружие, эскорт и т.п.\n\n"
        "Администрация может блокировать и скрывать объявления, нарушающие правила.",
        reply_markup=kb_help()
    )


@dp.message(F.text == "Как работает PartyRadar")
async def help_how(m: Message):
    await m.answer(
        "💡 <b>Как работает PartyRadar</b>\n\n"
        "1. Пользователи создают объявления (вечеринки, встречи, жильё, знакомства, маркет).\n"
        "2. Бот показывает их людям рядом по геолокации.\n"
        "3. Можно продвинуть объявление в ТОП, сделать PUSH-рассылку или баннер.\n\n"
        "Сервис помогает быстро находить движ в своём городе.",
        reply_markup=kb_help()
    )


@dp.message(F.text == "⬅ Назад")
async def back_global(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Главное меню:", reply_markup=kb_main())


@dp.message(F.text == "➕ Создать событие")
async def create_event_entry(m: Message, state: FSMContext):
    await state.set_state(AddEvent.type)
    await m.answer(
        "Выбери тип события:\n"
        "• 🎉 Вечеринка\n"
        "• 🏪 Маркет / услуги\n"
        "• 🏠 Жильё\n"
        "• 🫂 Знакомства\n"
        "• 🚗 Попутчики\n",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎉 Вечеринка"), KeyboardButton(text="🏪 Маркет")],
                [KeyboardButton(text="🏠 Жильё"), KeyboardButton(text="🫂 Знакомства")],
                [KeyboardButton(text="🚗 Попутчики")],
                [KeyboardButton(text="⬅ Назад")],
            ],
            resize_keyboard=True
        )
    )


EVENT_TYPES = {
    "🎉 Вечеринка": "party",
    "🏪 Маркет": "market",
    "🏠 Жильё": "rent",
    "🫂 Знакомства": "dating",
    "🚗 Попутчики": "rideshare",
}


@dp.message(AddEvent.type)
async def ev_type(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    if m.text not in EVENT_TYPES:
        return await m.answer("Выбери тип события из кнопок ниже.", reply_markup=kb_main())

    await state.update_data(event_type=EVENT_TYPES[m.text])

    await state.set_state(AddEvent.description)
    await m.answer(
        "📝 Опиши событие.\n\n"
        "Рекомендуем указать:\n"
        "• Формат и тему\n"
        "• Дату/время\n"
        "• Вход / цена\n"
        "• Адрес или район\n"
        "• Любые важные детали\n\n"
        "После описания отправь одним сообщением.",
        reply_markup=kb_back()
    )


@dp.message(AddEvent.description)
async def ev_description(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.type)
        return await m.answer(
            "Выбери тип события:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="🎉 Вечеринка"), KeyboardButton(text="🏪 Маркет")],
                    [KeyboardButton(text="🏠 Жильё"), KeyboardButton(text="🫂 Знакомства")],
                    [KeyboardButton(text="🚗 Попутчики")],
                    [KeyboardButton(text="⬅ Назад")],
                ],
                resize_keyboard=True
            )
        )

    desc = sanitize(m.text)
    if not desc:
        return await m.answer("Опиши событие текстом, пожалуйста.", reply_markup=kb_back())

    await state.update_data(description=desc)

    await state.set_state(AddEvent.media)
    await m.answer(
        "📸 Теперь добавь медиа:\n"
        "• Отправь фото/видео события\n"
        "• Или отправь геолокацию места\n\n"
        "Можно пропустить этот шаг, но объявления с медиа привлекают больше внимания.\n\n"
        "Если планируешь делать баннер — медиа нужно загрузить именно здесь.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.text == "⬅ Назад")
async def ev_media_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.description)
    await m.answer(
        "Вернулись к описанию.\n\n"
        "Опиши событие текстом (формат, дата/время, цена, адрес и детали).",
        reply_markup=kb_back()
    )


@dp.message(AddEvent.media, F.text.casefold() == "пропустить")
async def ev_media_skip(m: Message, state: FSMContext):
    await state.update_data(media_files=[])
    await state.set_state(AddEvent.contact)
    await m.answer(
        "Ок, без медиа.\n\n"
        "Теперь оставь способ связи (телеграм, телефон или ссылку).",
        reply_markup=kb_skip()
    )


@dp.message(AddEvent.media, F.photo | F.video | F.location)
async def ev_media(m: Message, state: FSMContext):
    data = await state.get_data()
    media_files = data.get("media_files") or []

    if m.photo:
        ph = m.photo[-1]
        media_files.append({"type": "photo", "file_id": ph.file_id})
    elif m.video:
        media_files.append({"type": "video", "file_id": m.video.file_id})
    elif m.location:
        await state.update_data(
            lat=m.location.latitude,
            lon=m.location.longitude
        )

    await state.update_data(media_files=media_files)

    await m.answer(
        "Медиа/локация сохранены.\n"
        "Можешь отправить ещё (будет использоваться первое медиа), или нажми «Пропустить», чтобы перейти дальше.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media)
async def ev_media_text(m: Message, state: FSMContext):
    if m.text == "Пропустить":
        await state.update_data(media_files=[])
        await state.set_state(AddEvent.contact)
        return await m.answer(
            "Ок, без медиа.\n\n"
            "Теперь оставь способ связи (телеграм, телефон или ссылку).",
            reply_markup=kb_skip()
        )

    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer(
            "Вернулись к описанию.\n\n"
            "Опиши событие текстом.",
            reply_markup=kb_back()
        )

    return await m.answer("Отправь фото/видео, геолокацию или нажми «Пропустить».", reply_markup=kb_media_step())


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
    "onlyfans.com",
    "pornhub.com",
    "1xbet",
    "1x-bet",
    "1xstavka",
]


def _check_forbidden(text: str) -> Optional[str]:
    lower = text.lower()
    for key, words in FORBIDDEN_KEYWORDS_GROUPS.items():
        for w in words:
            if w in lower:
                return key
    for d in FORBIDDEN_DOMAINS:
        if d in lower:
            return "domain"
    return None


@dp.message(AddEvent.lifetime)
async def ev_lifetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.contact)
        return await m.answer(
            "Вернулись к шагу контакта.\n\n"
            "Напиши, как с тобой связаться, или отправь «Пропустить».",
            reply_markup=kb_skip()
        )

    text = m.text or ""
    hours = None
    paid = False

    if "бесплатно" in text:
        hours = 24
        paid = False
    elif "3 дня" in text:
        hours = 72
        paid = True
    elif "7 дней" in text:
        hours = 168
        paid = True

    if hours is None:
        return await m.answer(
            "Выбери один из вариантов срока жизни объявления.",
            reply_markup=kb_lifetime()
        )

    await state.update_data(lifetime_hours=hours, lifetime_paid=paid)

    data = await state.get_data()
    desc = data.get("description", "")
    reason = _check_forbidden(desc)
    if reason:
        await state.clear()
        msg = "❌ Объявление не прошло автоматическую модерацию.\n\n"
        if reason == "adult":
            msg += "Похоже, в описании есть отсылки к интим-услугам. Это запрещено правилами."
        elif reason == "drugs":
            msg += "Похоже, в описании есть отсылки к наркотикам. Это запрещено."
        elif reason == "weapons":
            msg += "Похоже, в описании есть отсылки к оружию. Это запрещено."
        elif reason == "gambling":
            msg += "Похоже, в описании есть отсылки к азартным играм/ставкам. Это запрещено."
        elif reason == "fraud":
            msg += "Похоже, в описании есть формулировки про сомнительный заработок / пирамиды. Это запрещено."
        elif reason == "domain":
            msg += "В описании есть запрещённые ресурсы (adult/азартные/иное)."

        msg += "\n\nПопробуй переписать объявление без этих формулировок."
        return await m.answer(msg, reply_markup=kb_main())

    await state.set_state(AddEvent.confirm)
    await m.answer(
        "Проверь ещё раз объявление и нажми «✅ Опубликовать».\n\n"
        "Если хочешь что-то исправить — нажми «⬅ Назад».",
        reply_markup=kb_confirm()
    )


@dp.message(AddEvent.confirm)
async def ev_confirm(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.lifetime)
        return await m.answer(
            "Вернулись к выбору срока жизни объявления.",
            reply_markup=kb_lifetime()
        )

    if m.text == "❌ Отмена":
        await state.clear()
        return await m.answer("Создание объявления отменено.", reply_markup=kb_main())

    if m.text != "✅ Опубликовать":
        return await m.answer("Нажми «✅ Опубликовать» или «⬅ Назад».", reply_markup=kb_confirm())

    data = await state.get_data()
    events = _load_events()
    new_id = (events[-1]["id"] + 1) if events else 1
    now = datetime.now()
    lifetime_hours = data.get("lifetime_hours", 24)
    paid_lifetime = data.get("lifetime_paid", False)
    expire_at = now + timedelta(hours=lifetime_hours)

    event = {
        "id": new_id,
        "author": m.from_user.id,
        "type": data.get("event_type"),
        "description": data.get("description"),
        "media_files": data.get("media_files", []),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "contact": data.get("contact"),
        "created": now.isoformat(),
        "expire": expire_at.isoformat(),
        "is_paid_lifetime": paid_lifetime,
        "is_top": False,
        "top_expire": None,
        "top_paid_at": None,
        "status": "active",
    }

    events.append(event)
    _save_events(events)

    await state.set_state(AddEvent.upsell)
    await m.answer(
        "🎉 Объявление создано и опубликовано!\n\n"
        "Теперь можешь дополнительно продвинуть его:",
        reply_markup=kb_upsell()
    )


async def cc_create_invoice(amount: float, description: str, user_id: int, p_type: str, payload: Dict[str, Any]):
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
        logging.error("CryptoCloud API is not configured")
        return None

    import aiohttp

    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {
        "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": str(amount),
        "currency": "USD",
        "order_id": f"user_{user_id}_{int(datetime.now().timestamp())}",
        "description": description,
    }

    ext = {
        "user_id": user_id,
        "type": p_type,
        "payload": payload,
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=data) as resp:
                js = await resp.json()
                if js.get("success") and js.get("result"):
                    invoice = js["result"]
                    payments = _load_payments()
                    payments.append({
                        "uuid": invoice["id"],
                        "user_id": user_id,
                        "type": p_type,
                        "payload": payload,
                        "status": "created",
                        "created": datetime.now().isoformat(),
                    })
                    _save_payments(payments)
                    return {"uuid": invoice["id"], "link": invoice["link"]}
                else:
                    logging.error(f"CC create invoice error: {js}")
                    return None
        except Exception as e:
            logging.exception(f"CC create invoice exception: {e}")
            return None


async def cc_is_paid(invoice_uuid: str) -> bool:
    if not CRYPTOCLOUD_API_KEY:
        logging.error("CryptoCloud API is not configured")
        return False

    import aiohttp

    url = f"https://api.cryptocloud.plus/v2/invoice/info?uuid={invoice_uuid}"
    headers = {
        "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                js = await resp.json()
                if js.get("success") and js.get("result"):
                    status = js["result"].get("status")
                    if status == "paid":
                        payments = _load_payments()
                        for p in payments:
                            if p["uuid"] == invoice_uuid:
                                p["status"] = "paid"
                                p["paid_at"] = datetime.now().isoformat()
                                break
                        _save_payments(payments)
                        return True
                    return False
                else:
                    logging.error(f"CC invoice info error: {js}")
                    return False
        except Exception as e:
            logging.exception(f"CC invoice info exception: {e}")
            return False


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

    if txt == "⭐ Продвижение ТОП":
        await m.answer(
            "<b>⭐ТОП-продвижение</b> — поднимает твоё событие в начало списка, делая его заметным для всех пользователей.\n"
            "Это помогает быстрее собрать просмотры и отклики!\n"
        )

        await state.update_data(
            opt_type="top",
            opt_event_id=None,
            opt_days=None,
            _pay_uuid=None
        )
        await state.set_state(AddEvent.pay_option)
        return await m.answer("Выбери срок ТОП-продвижения:", reply_markup=kb_top_duration())

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
            "Стоимость указана ниже, нажми «💳 Получить ссылку на оплату».",
            reply_markup=kb_payment()
        )

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

        if media_files:
            f = media_files[0]
            b_media = {"type": f.get("type"), "file_id": f.get("file_id")}
        else:
            try:
                with open("assets/imgonline-com-ua-Resize-poVtNXt7aue6.png", "rb") as img:
                    sent = await m.bot.send_photo(m.chat.id, img, caption="")
                if sent.photo:
                    b_media = {"type": "photo", "file_id": sent.photo[-1].file_id}
                else:
                    b_media = None
                try:
                    await m.bot.delete_message(m.chat.id, sent.message_id)
                except Exception:
                    pass
            except Exception as e:
                logging.exception(f"Ошибка вставки fallback баннера: {e}")
                b_media = None

        parts = []
        if current.get("description"):
            parts.append(sanitize(current["description"]))

        b_text = "\n\n".join(parts) if parts else None

        await state.update_data(
            b_media=b_media,
            b_text=b_text,
            b_link=current.get("contact"),
            b_lat=current.get("lat"),
            b_lon=current.get("lon"),
            _pay_uuid=None
        )

        await state.set_state(AddBanner.duration)
        return await m.answer("Выбери срок показа баннера:", reply_markup=kb_banner_duration())

    await m.answer("Выбери вариант из меню ниже 👇", reply_markup=kb_upsell())


@dp.message(StateFilter(AddEvent.pay_option), F.text == "⬅ Назад")
async def ev_opt_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer(
        "Выбери дополнительную опцию или вернись в главное меню.",
        reply_markup=kb_upsell()
    )


@dp.message(StateFilter(AddEvent.pay_option))
async def ev_opt_router(m: Message, state: FSMContext):
    txt = m.text or ""
    data = await state.get_data()

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
            "Нажми «💳 Получить ссылку на оплату».",
            reply_markup=kb_payment()
        )

    if txt == "💳 Получить ссылку на оплату":
        opt_type = data.get("opt_type")
        ev_id = data.get("opt_event_id")
        days = data.get("opt_days")

        if opt_type == "top":
            if not days:
                return await m.answer("❌ Не выбран срок ТОП.", reply_markup=kb_top_duration())
            amount = TOP_PRICES.get(days)
            if not amount:
                return await m.answer("❌ Неверный срок ТОП.", reply_markup=kb_top_duration())
            description = f"TOP {days} days event {ev_id}"
        elif opt_type == "push":
            amount = PUSH_PRICE
            description = f"PUSH event {ev_id}"
        else:
            return await m.answer("❌ Тип опции не задан. Начни заново.", reply_markup=kb_upsell())

        invoice = await cc_create_invoice(
            amount=amount,
            description=description,
            user_id=m.from_user.id,
            p_type=opt_type,
            payload={"event_id": ev_id, "days": days}
        )
        if not invoice:
            return await m.answer("❌ Не удалось создать счёт. Попробуй позже.", reply_markup=kb_upsell())

        await state.update_data(_pay_uuid=invoice["uuid"])
        return await m.answer(
            f"👉 Перейди по ссылке для оплаты:\n{invoice['link']}\n\n"
            "После оплаты нажми «✅ Я оплатил».",
            reply_markup=kb_payment()
        )

    if txt == "✅ Я оплатил":
        data = await state.get_data()
        invoice_uuid = data.get("_pay_uuid")
        opt_type = data.get("opt_type")
        ev_id = data.get("opt_event_id")
        days = data.get("opt_days")

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
            if not days:
                return await m.answer("❌ Не задан срок ТОП.", reply_markup=kb_top_duration())
            target["is_top"] = True
            target["top_expire"] = (datetime.now() + timedelta(days=days)).isoformat()
            target["top_paid_at"] = datetime.now().isoformat()
            _save_events(events)
            await state.set_state(AddEvent.upsell_more)
            return await m.answer(
                f"🎉 ТОП активирован на {days} дней!\n\n"
                "Добавить ещё одну опцию к этому объявлению?",
                reply_markup=kb_upsell_more()
            )

        if opt_type == "push":
            sent = await send_push_for_event(target)
            await state.set_state(AddEvent.upsell_more)
            return await m.answer(
                f"📣 PUSH-рассылка отправлена. Получателей: {sent}.\n\n"
                "Добавить ещё одну опцию к этому объявлению?",
                reply_markup=kb_upsell_more()
            )

    await m.answer("Выбери пункт из меню.", reply_markup=kb_upsell())


@dp.message(AddEvent.upsell_more)
async def ev_upsell_more(m: Message, state: FSMContext):
    txt = m.text or ""

    if txt == "⬅ Завершить":
        await state.clear()
        return await m.answer("Готово! Возвращаю в главное меню.", reply_markup=kb_main())

    if txt == "⭐ Ещё ТОП":
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "Выбери ещё раз ТОП или другую опцию:",
            reply_markup=kb_upsell()
        )

    if txt == "📣 Ещё Push":
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "Выбери ещё раз Push или другую опцию:",
            reply_markup=kb_upsell()
        )

    if txt == "🖼 Ещё баннер":
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "Выбери ещё раз баннер или другую опцию:",
            reply_markup=kb_upsell()
        )

    await m.answer("Выбери вариант из меню ниже 👇", reply_markup=kb_upsell_more())


@dp.message(AddBanner.duration)
async def banner_duration(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.upsell)
        return await m.answer("Вернулись к выбору опций.", reply_markup=kb_upsell())

    if m.text not in BANNER_DURATIONS:
        return await m.answer("Выбери один из вариантов срока баннера.", reply_markup=kb_banner_duration())

    days, amount = BANNER_DURATIONS[m.text]

    await state.update_data(b_days=days, b_amount=amount)
    invoice = await cc_create_invoice(
        amount=amount,
        description=f"Banner {days} days",
        user_id=m.from_user.id,
        p_type="banner",
        payload={"days": days}
    )
    if not invoice:
        return await m.answer("❌ Не удалось создать счёт для баннера.", reply_markup=kb_upsell())

    await state.update_data(_pay_uuid=invoice["uuid"])
    await state.set_state(AddBanner.payment)
    await m.answer(
        f"👉 Ссылка на оплату баннера:\n{invoice['link']}\n\n"
        "После оплаты нажми «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("Вернулись к выбору срока баннера.", reply_markup=kb_banner_duration())


@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    if not uuid:
        return await m.answer("❌ Счёт не найден. Получи ссылку ещё раз.", reply_markup=kb_payment())

    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подожди и попробуй снова.", reply_markup=kb_payment())

    d = await state.get_data()
    media = d.get("b_media")
    text = d.get("b_text")
    link = d.get("b_link")
    lat = d.get("b_lat")
    lon = d.get("b_lon")
    days = d.get("b_days", 1)

    banners = _load_banners()
    new_id = (banners[-1]["id"] + 1) if banners else 1
    now = datetime.now()
    expire = now + timedelta(days=days)
    banners.append({
        "id": new_id,
        "owner": m.from_user.id,
        "media_type": media["type"] if isinstance(media, dict) else None,
        "file_id": media["file_id"] if isinstance(media, dict) else None,
        "text": text,
        "link": link,
        "lat": lat,
        "lon": lon,
        "created": now.isoformat(),
        "expire": expire.isoformat(),
        "notified": False,
    })
    _save_banners(banners)

    await state.set_state(AddEvent.upsell_more)
    await m.answer(
        "✅ Баннер активирован и будет показываться пользователям в твоём районе.\n\n"
        "Добавить ещё одну опцию к этому объявлению?",
        reply_markup=kb_upsell_more()
    )


@dp.message(F.text == "🔎 Найти события рядом")
async def find_events_near(m: Message, state: FSMContext):
    await m.answer(
        "Отправь свою геолокацию, чтобы найти события рядом.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
                [KeyboardButton(text="⬅ Назад")],
            ],
            resize_keyboard=True
        )
    )


@dp.message(F.location)
async def handle_location(m: Message, state: FSMContext):
    users = _load_users()
    found = False
    for u in users:
        if u["user_id"] == m.from_user.id:
            u["lat"] = m.location.latitude
            u["lon"] = m.location.longitude
            u["updated"] = datetime.now().isoformat()
            found = True
            break

    if not found:
        users.append({
            "user_id": m.from_user.id,
            "lat": m.location.latitude,
            "lon": m.location.longitude,
            "updated": datetime.now().isoformat(),
        })

    _save_users(users)

    await show_events_for_location(m, m.location.latitude, m.location.longitude)


async def show_events_for_location(m: Message, lat: float, lon: float):
    events = _load_events()
    now = datetime.now()

    events = [e for e in events if e.get("status") == "active"]
    events = [e for e in events if _safe_dt(e.get("expire")) and _safe_dt(e["expire"]) > now]

    for e in events:
        e_lat = e.get("lat")
        e_lon = e.get("lon")
        if e_lat is None or e_lon is None:
            e["distance"] = None
        else:
            e["distance"] = haversine(lat, lon, e_lat, e_lon)

    events = [e for e in events if e["distance"] is not None]
    events.sort(key=lambda x: (not x.get("is_top"), x["distance"]))

    if not events:
        return await m.answer("Рядом пока нет активных событий. Создай своё!", reply_markup=kb_main())

    for e in events:
        text_lines = []
        if e.get("is_top"):
            text_lines.append("🔥 <b>ТОП</b>")

        etype = e.get("type")
        if etype == "party":
            text_lines.append("🎉 <b>Вечеринка</b>")
        elif etype == "market":
            text_lines.append("🏪 <b>Маркет / услуги</b>")
        elif etype == "rent":
            text_lines.append("🏠 <b>Жильё</b>")
        elif etype == "dating":
            text_lines.append("🫂 <b>Знакомства</b>")
        elif etype == "rideshare":
            text_lines.append("🚗 <b>Попутчики</b>")

        text_lines.append(e.get("description", ""))

        dist = e.get("distance")
        if dist is not None:
            text_lines.append(f"📍 ~{int(dist)} км от тебя")

        if e.get("contact"):
            text_lines.append(f"☎️ {e['contact']}")

        txt = "\n\n".join(text_lines)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📍 Посмотреть на карте",
                        url=f"https://www.google.com/maps?q={e.get('lat')},{e.get('lon')}"
                    )
                ]
            ]
        )

        media_files = e.get("media_files") or []
        if media_files:
            f = media_files[0]
            if f["type"] == "photo":
                await m.answer_photo(f["file_id"], caption=txt, reply_markup=kb)
            elif f["type"] == "video":
                await m.answer_video(f["file_id"], caption=txt, reply_markup=kb)
            else:
                await m.answer(txt, reply_markup=kb)
        else:
            await m.answer(txt, reply_markup=kb)

    await m.answer("Это все события рядом. Можешь создать своё 👇", reply_markup=kb_main())


async def send_push_for_event(event: Dict[str, Any]) -> int:
    users = _load_users()
    if not users:
        return 0

    lat = event.get("lat")
    lon = event.get("lon")
    if lat is None or lon is None:
        return 0

    sent = 0
    for u in users:
        try:
            dist = haversine(lat, lon, u["lat"], u["lon"])
            if dist <= DEFAULT_RADIUS_KM:
                text_lines = []
                etype = event.get("type")
                if etype == "party":
                    text_lines.append("🎉 <b>Вечеринка</b>")
                elif etype == "market":
                    text_lines.append("🏪 <b>Маркет / услуги</b>")
                elif etype == "rent":
                    text_lines.append("🏠 <b>Жильё</b>")
                elif etype == "dating":
                    text_lines.append("🫂 <b>Знакомства</b>")
                elif etype == "rideshare":
                    text_lines.append("🚗 <b>Попутчики</b>")

                text_lines.append(event.get("description", ""))

                dist_km = int(dist)
                text_lines.append(f"📍 ~{dist_km} км от тебя")

                if event.get("contact"):
                    text_lines.append(f"☎️ {event['contact']}")

                txt = "\n\n".join(text_lines)

                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📍 Посмотреть на карте",
                                url=f"https://www.google.com/maps?q={event.get('lat')},{event.get('lon')}"
                            )
                        ]
                    ]
                )

                await bot.send_message(chat_id=u["user_id"], text=txt, reply_markup=kb)
                sent += 1
        except Exception as e:
            logging.exception(f"Ошибка PUSH пользователю {u['user_id']}: {e}")

    return sent


@dp.message(F.text == "💬 Чат в радиусе 10 км")
async def chat_radius(m: Message):
    await m.answer(
        "✉️ Напиши сообщение — мы отправим его активным пользователям в радиусе 10 км от твоей геолокации.\n\n"
        "Старайся быть вежливым и конкретным.",
        reply_markup=kb_back()
    )


@dp.message(F.text & ~StateFilter(AddEvent, AddBanner, PushBroadcast))
async def chat_radius_message(m: Message):
    if m.text == "⬅ Назад":
        return await m.answer("Главное меню:", reply_markup=kb_main())

    users = _load_users()
    if not users:
        return await m.answer("Пока нет активных пользователей поблизости.", reply_markup=kb_main())

    me = next((u for u in users if u["user_id"] == m.from_user.id), None)
    if not me or me.get("lat") is None or me.get("lon") is None:
        return await m.answer("Сначала отправь свою геолокацию через «🔎 Найти события рядом».", reply_markup=kb_main())

    sent = 0
    for u in users:
        if u["user_id"] == m.from_user.id:
            continue
        try:
            dist = haversine(me["lat"], me["lon"], u["lat"], u["lon"])
            if dist <= 10:
                await bot.send_message(
                    chat_id=u["user_id"],
                    text=f"💬 <b>Сообщение от пользователя рядом ({int(dist)} км)</b>:\n\n{m.text}"
                )
                sent += 1
        except Exception as e:
            logging.exception(f"Ошибка отправки сообщения в чат радиуса: {e}")

    await m.answer(f"✅ Сообщение отправлено {sent} пользователям в радиусе 10 км.", reply_markup=kb_main())


@dp.message(Command("admin"))
async def admin_panel(m: Message):
    if m.from_user.id not in ADMINS:
        return

    await m.answer(
        "Админ-панель:\n"
        "/stats — статистика\n"
        "/dump_events — дамп событий\n"
        "/dump_users — дамп пользователей"
    )


@dp.message(Command("stats"))
async def admin_stats(m: Message):
    if m.from_user.id not in ADMINS:
        return

    users = _load_users()
    events = _load_events()
    banners = _load_banners()
    payments = _load_payments()

    await m.answer(
        f"👥 Пользователи: {len(users)}\n"
        f"📣 События: {len(events)}\n"
        f"🖼 Баннеры: {len(banners)}\n"
        f"💳 Платежей: {len(payments)}"
    )


@dp.message(Command("dump_events"))
async def dump_events(m: Message):
    if m.from_user.id not in ADMINS:
        return

    events = _load_events()
    txt = json.dumps(events, ensure_ascii=False, indent=2)
    if len(txt) < 4000:
        await m.answer(f"<code>{txt}</code>")
    else:
        await m.answer_document(
            document=("events.json", txt.encode("utf-8")),
            caption="Дамп events.json"
        )


@dp.message(Command("dump_users"))
async def dump_users(m: Message):
    if m.from_user.id not in ADMINS:
        return

    users = _load_users()
    txt = json.dumps(users, ensure_ascii=False, indent=2)
    if len(txt) < 4000:
        await m.answer(f"<code>{txt}</code>")
    else:
        await m.answer_document(
            document=("users.json", txt.encode("utf-8")),
            caption="Дамп users.json"
        )


async def banners_daemon():
    while True:
        try:
            now = datetime.now()
            banners = _load_banners()
            changed = False
            for b in banners:
                exp = _safe_dt(b.get("expire"))
                if exp and exp <= now and b.get("status", "active") != "expired":
                    b["status"] = "expired"
                    changed = True

            if changed:
                _save_banners(banners)
        except Exception as e:
            logging.exception(f"Ошибка в banners_daemon: {e}")

        await asyncio.sleep(60)


async def cleanup_daemon():
    while True:
        try:
            now = datetime.now()

            events = _load_events()
            changed = False
            for ev in events:
                if ev.get("expire"):
                    dt = _safe_dt(ev["expire"])
                    if dt and dt <= now and ev.get("status") != "expired":
                        ev["status"] = "expired"
                        changed = True
            if changed:
                _save_events(events)
        except Exception as e:
            logging.exception(f"Ошибка cleanup_daemon(): {e}")

        await asyncio.sleep(60)


async def make_web_app():
    try:
        app = web.Application()
        app.router.add_get("/verification-25a55.txt", handle_unitpay_verification)

        app.router.add_post("/payment_callback", handle_payment_callback)
        app.router.add_get("/payment_callback", handle_payment_callback)

        SimpleRequestHandler(dispatcher=dp, bot=bot).register(
            app,
            path="/webhook"
        )

        return app
    except Exception as e:
            logging.exception(f"❌ Ошибка make_web_app(): {e}")
            return web.Application()


async def handle_payment_callback(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = await request.post()
        body = dict(body)

    logging.info(f"Payment callback: {body}")
    return web.Response(text="ok")


if __name__ == "__main__":
    from aiohttp import web
    port = int(os.getenv("PORT", 8000))
    web.run_app(app, host="0.0.0.0", port=port)
