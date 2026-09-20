from __future__ import annotations

import asyncio
import html
import os
import random
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().with_name(".env"))

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)

import db
import election
from config import (
    ADMIN_ID, BOT_TOKEN, DB_PATH, LIVE_EDIT_INTERVAL, PARTIES, PARTY_MARKERS, REGION_ASSIGN_WEIGHTS,
    REGIONS, STANDARD_ELECTION_SECONDS, TEST_ELECTION_SECONDS, TICK_INTERVAL,
    USER_SIGNAL_MAX, USER_SIGNAL_MIN,
)
from render import OUTPUT_PATH, render_map
from texts import mechanics_text, parties_text, region_text, start_text, wiki_url

router = Router()
REFRESH_LOCK = asyncio.Lock()
LAST_PUBLIC_REFRESH = 0.0


def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


def pick_region() -> str:
    regions = list(REGION_ASSIGN_WEIGHTS)
    weights = [REGION_ASSIGN_WEIGHTS[r] for r in regions]
    return random.choices(regions, weights=weights, k=1)[0]


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗳 Голосовать", callback_data="vote:open"), InlineKeyboardButton(text="📊 Лайв", callback_data="live")],
        [InlineKeyboardButton(text="🏛 Партии", callback_data="parties"), InlineKeyboardButton(text="🧭 Моя автономия", callback_data="myregion")],
        [InlineKeyboardButton(text="⚙️ Как голосовать", callback_data="mechanics"), InlineKeyboardButton(text="📚 Вики", url=wiki_url("Политические партии Кефирстана"))],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data="home")]])


def party_vote_kb() -> InlineKeyboardMarkup:
    rows = []
    codes = list(PARTIES)
    for i in range(0, len(codes), 2):
        row = []
        for code in codes[i:i+2]:
            row.append(InlineKeyboardButton(text=f"{PARTY_MARKERS[code]} {PARTIES[code].name}", callback_data=f"vote:pick:{code}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_vote_kb(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Да, {PARTY_MARKERS[code]} {PARTIES[code].name}", callback_data=f"vote:do:{code}")],
        [InlineKeyboardButton(text="Нет, обратно", callback_data="vote:open")],
    ])


def parties_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, p in PARTIES.items():
        rows.append([InlineKeyboardButton(text=f"{PARTY_MARKERS[code]} {p.name} - открыть вики", url=wiki_url(p.wiki_title))])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Открыть 3 дня", callback_data="adm:open3d"), InlineKeyboardButton(text="⚡ Тест 1 мин", callback_data="adm:test")],
        [InlineKeyboardButton(text="⛔ Закрыть", callback_data="adm:close"), InlineKeyboardButton(text="🧹 Сброс", callback_data="adm:reset")],
        [InlineKeyboardButton(text="💉 Вкинуть голоса", callback_data="adm:inject"), InlineKeyboardButton(text="🎚 Подкрутить шанс", callback_data="adm:signal")],
        [InlineKeyboardButton(text="🗺 Обновить лайв", callback_data="adm:refresh"), InlineKeyboardButton(text="📈 Статус", callback_data="adm:status")],
    ])


def choose_region_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="КАР", callback_data=f"{prefix}:KAR"), InlineKeyboardButton(text="ТАР", callback_data=f"{prefix}:TAR"), InlineKeyboardButton(text="МАР", callback_data=f"{prefix}:MAR")],
        [InlineKeyboardButton(text="← Админка", callback_data="adm:home")],
    ])


def choose_party_kb(prefix: str, region: str) -> InlineKeyboardMarkup:
    rows = []
    codes = list(PARTIES)
    for i in range(0, len(codes), 2):
        rows.append([InlineKeyboardButton(text=f"{PARTY_MARKERS[c]} {PARTIES[c].name}", callback_data=f"{prefix}:{region}:{c}") for c in codes[i:i+2]])
    rows.append([InlineKeyboardButton(text="← Админка", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choose_amount_kb(prefix: str, region: str, party: str, amounts: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{n:,}".replace(",", " "), callback_data=f"{prefix}:{region}:{party}:{n}") for n in amounts[:3]],
        [InlineKeyboardButton(text=f"{n:,}".replace(",", " "), callback_data=f"{prefix}:{region}:{party}:{n}") for n in amounts[3:]],
        [InlineKeyboardButton(text="← Админка", callback_data="adm:home")],
    ])


async def ensure_user(message_or_cb) -> dict:
    u = message_or_cb.from_user
    user = await db.get_user(u.id)
    region = user["region"] if user else pick_region()
    return await db.upsert_user(u.id, u.username, u.first_name, region)


def fmt_time_left(snapshot: dict) -> str:
    e = snapshot["election"]
    if e["status"] != "active":
        return "голосование закрыто"
    left = max(0, int(float(e["end_ts"]) - time.time()))
    if left >= 86400:
        return f"осталось {left//86400} д. {(left%86400)//3600} ч."
    if left >= 3600:
        return f"осталось {left//3600} ч. {(left%3600)//60} мин."
    return f"осталось {left//60} мин. {left%60} сек."


def build_caption(s: dict) -> str:
    e = s["election"]
    status = "ИДЕТ" if e["status"] == "active" else "ЗАКРЫТО"
    lines = [f"🗳 <b>ВЫБОРЫ КЕФИРСТАНА 2059 - {status}</b>", fmt_time_left(s)]

    for r in ("KAR", "TAR", "MAR"):
        sh = s["shares"][r]
        ordered = sorted(PARTIES, key=lambda p: sh[p], reverse=True)
        leader, runner = ordered[0], ordered[1]
        margin = (sh[leader] - sh[runner]) * 100
        rs = s["regions"][r]
        turnout_now = min(99.0, rs["counted"] / max(1, rs["electorate"]) * 100)

        lines.append(
            f"\n<b>{REGIONS[r]['abbr']}</b> · явка {turnout_now:.1f}% · "
            f"лидер {PARTY_MARKERS[leader]} <b>{PARTIES[leader].name}</b> +{margin:.1f} п.п."
        )

        # Разбиваем длинную сводку на 2 строки. Так взгляд не ломается об стену процентов.
        first = ordered[:4]
        second = ordered[4:]
        lines.append(" · ".join(
            f"{PARTY_MARKERS[p]} {PARTIES[p].name} {sh[p]*100:.1f}%" for p in first
        ))
        if second:
            lines.append(" · ".join(
                f"{PARTY_MARKERS[p]} {PARTIES[p].name} {sh[p]*100:.1f}%" for p in second
            ))

    lines.append("\n<b>ЙАР</b> · N/D")

    if s["seats"]:
        seats = s["seats"]
        top = sorted(PARTIES, key=lambda p: seats[p], reverse=True)
        seat_bits = " · ".join(
            f"{PARTY_MARKERS[p]} {PARTIES[p].name} {seats[p]}" for p in top if seats[p] > 0
        )
        if seats.get("IND", 0):
            seat_bits += f" · ⬜ НЕЗ {seats['IND']}"
        lines.append(f"\n<b>Если закрыть сейчас:</b> {seat_bits}")

    text = "\n".join(lines)
    # Лимит подписи Telegram 1024. Сначала убираем проекцию мест, если вдруг разрослось.
    if len(text) > 1010 and s["seats"]:
        cut = text.rfind("\n<b>Если закрыть сейчас:</b>")
        if cut > 0:
            text = text[:cut]
    return text[:1010]


async def create_snapshot_file() -> tuple[dict, str, str]:
    s = await election.snapshot()
    await db.bump_snapshot()
    path = render_map(s, OUTPUT_PATH)
    caption = build_caption(s)
    return s, path, caption


async def refresh_all(bot: Bot, force: bool = False) -> None:
    global LAST_PUBLIC_REFRESH
    async with REFRESH_LOCK:
        now = time.time()
        if not force and now - LAST_PUBLIC_REFRESH < LIVE_EDIT_INTERVAL:
            return
        LAST_PUBLIC_REFRESH = now
        s, path, caption = await create_snapshot_file()
        posts = await db.get_live_posts()
        for post in posts:
            try:
                media = InputMediaPhoto(media=FSInputFile(path), caption=caption, parse_mode=ParseMode.HTML)
                await bot.edit_message_media(chat_id=post["chat_id"], message_id=post["message_id"], media=media)
            except TelegramBadRequest as ex:
                # "message is not modified" нам не мешает. Все остальное попробуем пережить до следующего тика.
                if "message is not modified" not in str(ex).lower():
                    pass
            except TelegramForbiddenError:
                await db.disable_live_post(post["chat_id"])
            except Exception as ex:
                print("live edit:", repr(ex))


async def send_live(bot: Bot, chat_id: int, chat_type: str) -> Message:
    _, path, caption = await create_snapshot_file()
    msg = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(path), caption=caption, parse_mode=ParseMode.HTML)
    await db.add_live_post(chat_id, msg.message_id, chat_type)
    return msg


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await ensure_user(message)
    await message.answer(start_text(user["region"]), reply_markup=main_kb())


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    user = await ensure_user(cb)
    await cb.message.edit_text(start_text(user["region"]), reply_markup=main_kb())
    await cb.answer()


@router.callback_query(F.data == "parties")
async def cb_parties(cb: CallbackQuery):
    await cb.message.edit_text(parties_text(), reply_markup=parties_kb())
    await cb.answer()


@router.callback_query(F.data == "mechanics")
async def cb_mechanics(cb: CallbackQuery):
    await cb.message.edit_text(mechanics_text(), reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "myregion")
async def cb_myregion(cb: CallbackQuery):
    user = await ensure_user(cb)
    await cb.message.edit_text(region_text(user["region"]), reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "live")
async def cb_live(cb: CallbackQuery, bot: Bot):
    await send_live(bot, cb.message.chat.id, cb.message.chat.type)
    await cb.answer("Лайв включен. Это сообщение теперь будет обновляться само")


@router.callback_query(F.data == "vote:open")
async def cb_vote_open(cb: CallbackQuery):
    user = await ensure_user(cb)
    e = await db.get_election()
    if e["status"] != "active":
        await cb.answer("Голосование сейчас закрыто", show_alert=True)
        return
    if user["voted_party"]:
        await cb.answer(f"Ты уже проголосовал: {PARTIES[user['voted_party']].name}", show_alert=True)
        return
    await cb.message.edit_text(
        f"<b>{REGIONS[user['region']]['abbr']} - твой бюллетень</b>\n\nВыбери партию. После подтверждения обычной кнопки 'передумал' уже не будет",
        reply_markup=party_vote_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("vote:pick:"))
async def cb_vote_pick(cb: CallbackQuery):
    code = cb.data.split(":")[-1]
    p = PARTIES.get(code)
    if not p:
        return await cb.answer("Партия не найдена", show_alert=True)
    await cb.message.edit_text(
        f"<b>{p.name}</b>\n{html.escape(p.short)}\n\nПодтверждаешь?",
        reply_markup=confirm_vote_kb(code),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("vote:do:"))
async def cb_vote_do(cb: CallbackQuery, bot: Bot):
    code = cb.data.split(":")[-1]
    if code not in PARTIES:
        return await cb.answer("Партия не найдена", show_alert=True)
    e = await db.get_election()
    if e["status"] != "active":
        return await cb.answer("Уже закрыто", show_alert=True)
    user = await ensure_user(cb)
    weight = random.SystemRandom().uniform(USER_SIGNAL_MIN, USER_SIGNAL_MAX)
    ok, _ = await db.cast_user_signal(cb.from_user.id, code, weight)
    if not ok:
        u = await db.get_user(cb.from_user.id)
        return await cb.answer(f"Голос уже записан: {PARTIES[u['voted_party']].name}", show_alert=True)
    await cb.message.edit_text(
        f"<b>Голос принят</b>\n\n{REGIONS[user['region']]['abbr']} · {PARTIES[code].name}\n\nВсе. Бюллетень ушел в подсчет твоей автономии. Теперь остается смотреть лайв и надеяться что остальные не проголосуют как идиоты",
        reply_markup=main_kb(),
    )
    await cb.answer("Записано")
    await refresh_all(bot, force=True)


@router.message(Command("live"))
async def cmd_live(message: Message, bot: Bot):
    await ensure_user(message)
    await send_live(bot, message.chat.id, message.chat.type)


@router.message(Command("parties"))
async def cmd_parties(message: Message):
    await message.answer(parties_text(), reply_markup=parties_kb())


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("<b>Скрытая админка 2059</b>\nТут уже можно нормально издеваться над моделью", reply_markup=admin_kb())


@router.message(Command("adminhelp"))
async def cmd_adminhelp(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Команды админа</b>\n"
        "/open3d - открыть на 3 дня\n"
        "/test1m - тест на 1 минуту\n"
        "/close - закрыть и досчитать\n"
        "/reset - полный сброс выборов\n"
        "/inject KAR LPK 1000 - прямой вкид бюллетеней\n"
        "/signal TAR PNEK 10 - подкрутить вероятность будущих голосов\n"
        "/setturnout MAR 64 - цель явки 64%\n"
        "/setregion USER_ID KAR - сменить человеку автономию\n"
        "/unvote USER_ID - стереть его выбор\n"
        "/setduration 30 - закончить через 30 минут\n"
        "/refresh - сразу обновить все лайвы\n"
        "/stats - статистика бота\n"
        "/export - прислать sqlite базу"
    )


async def admin_open(message: Message, bot: Bot, duration: int, test: bool):
    await election.open_election(duration, test)
    await message.answer("Выборы открыты" + (" в тестовом режиме на 1 минуту" if test else " на 3 дня"))
    await refresh_all(bot, force=True)


@router.message(Command("open3d"))
async def cmd_open3d(message: Message, bot: Bot):
    if is_admin(message.from_user.id):
        await admin_open(message, bot, STANDARD_ELECTION_SECONDS, False)


@router.message(Command("test1m"))
async def cmd_test1m(message: Message, bot: Bot):
    if is_admin(message.from_user.id):
        await admin_open(message, bot, TEST_ELECTION_SECONDS, True)


@router.message(Command("close"))
async def cmd_close(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    await election.close_election()
    await message.answer("Закрыл. Финальный поток досчитан")
    await refresh_all(bot, force=True)


@router.message(Command("reset"))
async def cmd_reset(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    await db.reset_election_data()
    await message.answer("Сбросил результаты, сигналы и пользовательские голоса. Автономии у людей оставил")
    await refresh_all(bot, force=True)


@router.message(Command("inject"))
async def cmd_inject(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 4:
        return await message.answer("/inject KAR LPK 1000")
    region, party = parts[1].upper(), parts[2].upper()
    if region not in ("KAR","TAR","MAR") or party not in PARTIES:
        return await message.answer("Регион: KAR/TAR/MAR. Партия: " + "/".join(PARTIES))
    try: amount = int(parts[3].replace(" ",""))
    except ValueError: return await message.answer("Количество должно быть числом")
    actual = await db.inject_ballots(region, party, amount, message.from_user.id)
    await message.answer(f"Вкинул {actual:,} за {PARTIES[party].name} в {REGIONS[region]['abbr']}".replace(",", " "))
    await refresh_all(bot, force=True)


@router.message(Command("signal"))
async def cmd_signal(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 4:
        return await message.answer("/signal TAR PNEK 10")
    region, party = parts[1].upper(), parts[2].upper()
    if region not in ("KAR","TAR","MAR") or party not in PARTIES:
        return await message.answer("Не понял регион или партию")
    try: power = float(parts[3].replace(",","."))
    except ValueError: return await message.answer("Сила должна быть числом")
    await db.add_signal(region, party, power, message.from_user.id)
    await message.answer(f"Шанс {PARTIES[party].name} в {REGIONS[region]['abbr']} подкручен на {power:g}")
    await refresh_all(bot, force=True)


@router.message(Command("setturnout"))
async def cmd_setturnout(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 3: return await message.answer("/setturnout MAR 64")
    region = parts[1].upper()
    if region not in ("KAR","TAR","MAR"): return await message.answer("KAR/TAR/MAR")
    try: pct = float(parts[2].replace(",","."))
    except ValueError: return await message.answer("Процент числом")
    await db.set_turnout_target(region, pct/100)
    await message.answer(f"Целевая явка {REGIONS[region]['abbr']} = {pct:.1f}%")
    await refresh_all(bot, force=True)


@router.message(Command("setregion"))
async def cmd_setregion(message: Message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 3: return await message.answer("/setregion 123456789 KAR")
    try: uid = int(parts[1])
    except ValueError: return await message.answer("USER_ID числом")
    region = parts[2].upper()
    if region not in ("KAR","TAR","MAR"): return await message.answer("KAR/TAR/MAR")
    await db.revoke_user_vote(uid)
    ok = await db.set_user_region(uid, region)
    await message.answer("Готово. Старый голос если был тоже стер" if ok else "Такого пользователя еще нет в базе")


@router.message(Command("unvote"))
async def cmd_unvote(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 2: return await message.answer("/unvote USER_ID")
    try: uid = int(parts[1])
    except ValueError: return await message.answer("USER_ID числом")
    ok = await db.revoke_user_vote(uid)
    await message.answer("Голос стер" if ok else "У него нет записанного голоса")
    await refresh_all(bot, force=True)


@router.message(Command("setduration"))
async def cmd_setduration(message: Message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 2: return await message.answer("/setduration 30")
    try: mins = max(1, int(parts[1]))
    except ValueError: return await message.answer("Минуты числом")
    e = await db.get_election()
    if e["status"] != "active": return await message.answer("Сначала открой выборы")
    await db.set_election(end_ts=time.time() + mins*60)
    await message.answer(f"Теперь до закрытия {mins} мин.")


@router.message(Command("refresh"))
async def cmd_refresh(message: Message, bot: Bot):
    if not is_admin(message.from_user.id): return
    await refresh_all(bot, force=True)
    await message.answer("Лайвы обновлены")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id): return
    st = await db.stats(); e = await db.get_election(); rs = await db.get_region_rows()
    lines = [f"Статус: {e['status']}", f"Пользователей: {st['users']}", f"Проголосовали: {st['voted']}", f"Лайв-сообщений: {st['live_posts']}"]
    for r in ("KAR","TAR","MAR"):
        lines.append(f"{REGIONS[r]['abbr']}: counted {rs[r]['counted']} / turnout target {rs[r]['turnout_target']*100:.1f}%")
    await message.answer("\n".join(lines))


@router.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message.from_user.id): return
    if not Path(DB_PATH).exists(): return await message.answer("Базы пока нет")
    await message.answer_document(FSInputFile(DB_PATH), caption="Текущая база выборов")


@router.callback_query(F.data == "adm:home")
async def adm_home(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await cb.message.edit_text("<b>Скрытая админка 2059</b>", reply_markup=admin_kb())
    await cb.answer()


@router.callback_query(F.data == "adm:open3d")
async def adm_open3d(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await election.open_election(STANDARD_ELECTION_SECONDS, False)
    await cb.answer("Открыто на 3 дня", show_alert=True)
    await refresh_all(bot, force=True)


@router.callback_query(F.data == "adm:test")
async def adm_test(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await election.open_election(TEST_ELECTION_SECONDS, True)
    await cb.answer("Тест на 1 минуту запущен", show_alert=True)
    await refresh_all(bot, force=True)


@router.callback_query(F.data == "adm:close")
async def adm_close(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await election.close_election(); await refresh_all(bot, force=True)
    await cb.answer("Закрыто", show_alert=True)


@router.callback_query(F.data == "adm:reset")
async def adm_reset(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await db.reset_election_data(); await refresh_all(bot, force=True)
    await cb.answer("Результаты сброшены", show_alert=True)


@router.callback_query(F.data == "adm:inject")
async def adm_inject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await cb.message.edit_text("Куда вкидываем?", reply_markup=choose_region_kb("admir")); await cb.answer()


@router.callback_query(F.data.startswith("admir:"))
async def adm_inject_region(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    region = cb.data.split(":")[1]
    await cb.message.edit_text(f"{REGIONS[region]['abbr']}. За кого?", reply_markup=choose_party_kb("admip", region)); await cb.answer()


@router.callback_query(F.data.startswith("admip:"))
async def adm_inject_party(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    _, region, party = cb.data.split(":")
    await cb.message.edit_text(f"{REGIONS[region]['abbr']} · {PARTIES[party].name}. Сколько бюллетеней?", reply_markup=choose_amount_kb("admido", region, party, [100,1000,5000,10000,25000])); await cb.answer()


@router.callback_query(F.data.startswith("admido:"))
async def adm_inject_do(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    _, region, party, amount = cb.data.split(":")
    actual = await db.inject_ballots(region, party, int(amount), cb.from_user.id)
    await cb.answer(f"Вкинул {actual}", show_alert=True); await refresh_all(bot, force=True)


@router.callback_query(F.data == "adm:signal")
async def adm_signal(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await cb.message.edit_text("Где крутим шанс?", reply_markup=choose_region_kb("admsr")); await cb.answer()


@router.callback_query(F.data.startswith("admsr:"))
async def adm_signal_region(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    region = cb.data.split(":")[1]
    await cb.message.edit_text(f"{REGIONS[region]['abbr']}. Кому?", reply_markup=choose_party_kb("admsp", region)); await cb.answer()


@router.callback_query(F.data.startswith("admsp:"))
async def adm_signal_party(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    _, region, party = cb.data.split(":")
    await cb.message.edit_text(f"{REGIONS[region]['abbr']} · {PARTIES[party].name}. Сила сигнала?", reply_markup=choose_amount_kb("admsdo", region, party, [1,5,10,25,50])); await cb.answer()


@router.callback_query(F.data.startswith("admsdo:"))
async def adm_signal_do(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    _, region, party, amount = cb.data.split(":")
    await db.add_signal(region, party, float(amount), cb.from_user.id)
    await cb.answer(f"Шанс подкручен на {amount}", show_alert=True); await refresh_all(bot, force=True)


@router.callback_query(F.data == "adm:refresh")
async def adm_refresh(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer()
    await refresh_all(bot, force=True); await cb.answer("Обновлено", show_alert=True)


@router.callback_query(F.data == "adm:status")
async def adm_status(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer()
    st = await db.stats(); e = await db.get_election()
    await cb.answer(f"{e['status']} · users {st['users']} · votes {st['voted']} · live {st['live_posts']}", show_alert=True)


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot):
    if event.chat.type != ChatType.CHANNEL:
        return
    new_status = event.new_chat_member.status
    if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        try:
            await send_live(bot, event.chat.id, "channel")
        except Exception:
            pass
    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.disable_live_post(event.chat.id)


async def ticker(bot: Bot):
    while True:
        try:
            e = await db.get_election()
            if e["status"] == "active":
                changed = await election.advance()
                if changed:
                    await refresh_all(bot, force=False)
        except Exception as ex:
            print("ticker:", repr(ex))
        await asyncio.sleep(TICK_INTERVAL)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пуст. Создай .env или передай переменную окружения BOT_TOKEN")
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    task = asyncio.create_task(ticker(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
