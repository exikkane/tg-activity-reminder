import asyncio
import html
import logging
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .duration import InvalidDuration, human, parse_duration
from .free_time import FreeTime
from .preferences import Preferences
from .reports import Reports
from .youtrack import Reconnect, ServiceError, Uncertain, require_visible_stage, spent

log = logging.getLogger(__name__)


def clean(text, limit=180):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape(text, limit=180):
    return html.escape(clean(text, limit))


class UI:
    def __init__(self, settings, db, yt, work, bot: Bot):
        self.s, self.db, self.yt, self.work, self.bot = settings, db, yt, work, bot
        self.local = FreeTime(self)
        self.reports = Reports(self)
        self.preferences = Preferences(self)
        self.dp = Dispatcher()
        router = Router()
        router.message.filter(F.chat.type == "private")
        router.callback_query.filter(F.message.chat.type == "private")
        router.message.register(
            self.command,
            Command(
                "start",
                "tasks",
                "account",
                "disconnect",
                "free",
                "report",
                "teamreport",
                "cancel",
                "settings",
            ),
        )
        router.callback_query.register(self.callback)
        router.message.register(self.text, F.text)
        self.dp.include_router(router)
        self.locks = defaultdict(asyncio.Lock)

    def button(self, tg, label, kind, data=None):
        return InlineKeyboardButton(text=label, callback_data=self.db.action(tg, kind, data))

    def report_button(self, tg):
        return self.button(
            tg, "📊 Месячный отчёт", "team_report" if self.s.report_admin(tg) else "free_report"
        )

    def keys(self, *rows):
        return InlineKeyboardMarkup(inline_keyboard=list(rows))

    async def show(self, tg, text, keys=None, message=None):
        if message:
            try:
                await self.bot.edit_message_text(
                    chat_id=tg,
                    message_id=message.message_id,
                    text=text,
                    reply_markup=keys,
                    parse_mode="HTML",
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message.lower():
                    return
                # A deleted/old message may no longer be editable.
                log.info("telegram_edit_unavailable")
        await self.bot.send_message(tg, text, reply_markup=keys, parse_mode="HTML")

    async def connect(self, tg, message=None):
        state = self.db.new_oauth(tg)
        await self.show(
            tg,
            "<b>Добро пожаловать! 👋</b>\n\nЯ помогу тебе вести учёт рабочего времени в YouTrack.\n\nДля начала необходимо привязать свой аккаунт.",
            self.keys(
                [
                    InlineKeyboardButton(
                        text="🔗 Подключить YouTrack",
                        url=self.s.app_base_url + "/connect?ticket=" + state,
                    )
                ]
            ),
            message,
        )

    async def guard(self, tg):
        if not self.s.allowed(tg):
            raise ServiceError("Этот бот доступен только участникам команды.")
        user = self.db.user(tg)
        if not user or not user["active"]:
            raise Reconnect()
        return user

    async def failure(self, tg, exc, message=None):
        if isinstance(exc, Reconnect):
            await self.connect(tg, message)
        elif isinstance(exc, Uncertain):
            await self.show(
                tg,
                escape(str(exc), 600),
                self.keys(
                    [self.button(tg, "🔄 Проверить запись", "reconcile", {"op": exc.op_id})],
                    [self.button(tg, "📋 К списку задач", "list")],
                ),
                message,
            )
        else:
            await self.show(tg, escape(str(exc), 600), message=message)

    async def command(self, message: Message):
        tg = message.from_user.id
        if not self.s.allowed(tg):
            await message.answer("Этот бот доступен только участникам команды.")
            return
        async with self.locks[tg]:
            command = message.text.split()[0].split("@")[0]
            try:
                if command == "/settings":
                    parts = message.text.split()
                    if len(parts) > 2:
                        raise ValueError("Используй /settings или /settings 19:30.")
                    if len(parts) == 2:
                        await self.preferences.save(tg, parts[1])
                    else:
                        await self.preferences.show(tg)
                elif command == "/free":
                    await self.local.begin(tg)
                elif command in {"/report", "/teamreport"}:
                    parts = message.text.split()
                    if len(parts) > 2:
                        raise ValueError("Укажи месяц в формате /report 2026-11.")
                    self.db.clear_conversation(tg)
                    report = (
                        self.reports.show
                        if command == "/teamreport" or self.s.report_admin(tg)
                        else self.local.report
                    )
                    await report(tg, parts[1] if len(parts) == 2 else None)
                elif command == "/cancel":
                    self.local.user(tg)
                    self.db.clear_conversation(tg)
                    await self.show(tg, "Ввод отменён.", self.local.buttons(tg))
                elif command == "/start":
                    self.db.execute("UPDATE users SET blocked=0 WHERE tg=?", (tg,))
                    user = self.db.user(tg)
                    if not user or not user["active"]:
                        await self.connect(tg)
                    else:
                        await self.list_tasks(tg)
                elif command == "/disconnect":
                    # Available even when access expired.
                    if not self.db.user(tg):
                        await self.show(tg, "YouTrack не подключён. Используй /start.")
                    else:
                        await self.show(
                            tg,
                            "Отключить аккаунт YouTrack? Записи времени в YouTrack сохранятся.",
                            self.keys(
                                [self.button(tg, "Да, отключить", "disconnect")],
                                [self.button(tg, "❌ Отмена", "list")],
                            ),
                        )
                else:
                    user = await self.guard(tg)
                    self.db.clear_conversation(tg)
                    if command == "/account":
                        await self.show(
                            tg,
                            f"<b>✅ YouTrack подключён</b>\n\nАккаунт: {escape(user['name'])}\nЛогин: {escape(user['login'])}\nЧасовой пояс: {escape(user['zone'])}",
                            self.keys([self.button(tg, "📋 Мои задачи", "list")]),
                        )
                    else:
                        await self.list_tasks(tg)
            except (ServiceError, ValueError) as exc:
                await self.failure(tg, exc)

    async def list_tasks(self, tg, page=0, search=None, message=None, reminder=None):
        await self.guard(tg)
        items = await self.yt.issues(tg, page, search)
        context = {"page": page, "search": search}
        if reminder:
            months = (
                "января",
                "февраля",
                "марта",
                "апреля",
                "мая",
                "июня",
                "июля",
                "августа",
                "сентября",
                "октября",
                "ноября",
                "декабря",
            )
            title = f"<b>🔔 Запиши итоги рабочего дня</b>\n\nСегодня {reminder.day} {months[reminder.month - 1]}.\n\n"
        else:
            title = "<b>🔎 Результаты поиска</b>\n\n" if search else "<b>📋 Мои задачи</b>\n\n"
        rows = []
        if items:
            title += "\n".join(
                f"{page * 10 + i + 1}. {escape(item['idReadable'])} — {escape(item['summary'], 130)}"
                for i, item in enumerate(items[:10])
            )
            for i, item in enumerate(items[:10]):
                rows.append(
                    [
                        self.button(
                            tg,
                            f"{page * 10 + i + 1}. {clean(item['summary'], 48)}",
                            "card",
                            dict(context, issue=item["id"]),
                        )
                    ]
                )
        else:
            title += (
                "Ничего не найдено."
                if search
                else "Сейчас у тебя нет задач в статусах Ready to work, In progress или Ready for review.\n\nТы можешь найти другую задачу в этих статусах."
            )
        nav = []
        if page:
            nav.append(self.button(tg, "⬅️ Назад", "list", dict(context, page=page - 1)))
        if len(items) > 10:
            nav.append(self.button(tg, "Вперёд ➡️", "list", dict(context, page=page + 1)))
        if nav:
            rows.append(nav)
        rows.append([self.button(tg, "➕ Добавить другую задачу", "search")])
        rows.append([self.button(tg, "✍️ Записать работу без YouTrack", "free")])
        rows.append([self.report_button(tg)])
        rows.append([self.button(tg, "🔔 Время напоминания", "settings")])
        if search:
            rows.append([self.button(tg, "📋 Мои задачи", "list")])
        await self.show(tg, title, self.keys(*rows), message)

    async def card(self, tg, data, message=None):
        issue = await self.yt.issue(tg, data["issue"])
        require_visible_stage(issue)
        responsible = "Не назначен"
        reviewer = None
        for field in issue.get("customFields", []):
            if field["name"] in {"Responsible", "Assignee", "Проверяющий"} and field.get("value"):
                values = field["value"] if isinstance(field["value"], list) else [field["value"]]
                names = ", ".join(
                    v.get("fullName") or v.get("name") or v.get("login", "") for v in values
                )
                if field["name"] == "Проверяющий":
                    reviewer = names
                else:
                    responsible = names
        total = spent(issue)
        text = f"<b>📋 {escape(issue['idReadable'])} — {escape(issue['summary'], 350)}</b>\n\nПроект: {escape(issue['project']['name'])}\nИсполнитель: {escape(responsible)}"
        if reviewer:
            text += f"\nПроверяющий: {escape(reviewer)}"
        text += "\nПотрачено всего: " + (human(total) if total is not None else "недоступно")
        context = dict(data, issue=issue["id"])
        await self.show(
            tg,
            text,
            self.keys(
                [
                    InlineKeyboardButton(
                        text="🔗 Перейти в задачу",
                        url=self.s.youtrack_base_url
                        + "/issue/"
                        + quote(issue["idReadable"], safe=""),
                    )
                ],
                [self.button(tg, "⏱ Записать время", "time", context)],
                [
                    self.button(
                        tg,
                        "⬅️ К списку задач",
                        "list",
                        {k: v for k, v in data.items() if k in {"page", "search"}},
                    )
                ],
            ),
            message,
        )

    async def success(self, tg, op, message=None):
        # The committed result is independent of subsequent total-fetch/Telegram failures.
        try:
            issue = await self.yt.issue(tg, op["issue"])
            total = spent(issue)
            heading = f"{escape(issue['idReadable'])} — {escape(issue['summary'], 250)}"
            tail = human(total) if total is not None else "недоступно"
        except ServiceError:
            heading = "Запись сохранена в YouTrack"
            tail = "пока не удалось загрузить"
        self.db.clear_conversation(tg)
        await self.show(
            tg,
            f"<b>✅ Время записано!</b>\n\n{heading}\n\nДобавлено: <b>{human(op['minutes'])}</b>\nВсего по задаче: <b>{tail}</b>",
            self.keys(
                [self.button(tg, "➕ Добавить ещё время", "time", {"issue": op["issue"]})],
                [self.button(tg, "📋 К списку задач", "list")],
            ),
            message,
        )

    async def callback(self, callback: CallbackQuery):
        tg = callback.from_user.id
        await callback.answer()
        if not self.s.allowed(tg):
            return
        async with self.locks[tg]:
            try:
                action = self.db.get_action(callback.data, tg)
                kind, data = action["kind"], action["data"]
                if kind in {"settings", "settings_time"}:
                    if kind == "settings":
                        await self.preferences.show(tg, callback.message)
                    else:
                        await self.preferences.begin(tg, callback.message)
                    return
                if kind in {"team_report", "team_export"}:
                    self.reports.authorize(tg)
                    self.db.clear_conversation(tg)
                    await self.reports.show(
                        tg,
                        data.get("month"),
                        callback.message,
                        source=data.get("source", "all"),
                        download=kind == "team_export",
                    )
                    return
                if kind in {"free", "free_cancel", "free_report", "free_export"}:
                    self.local.user(tg)
                    if kind == "free":
                        await self.local.begin(tg, callback.data, callback.message)
                    elif kind == "free_cancel":
                        self.db.clear_conversation(tg)
                        await self.show(
                            tg, "Ввод отменён.", self.local.buttons(tg), callback.message
                        )
                    else:
                        self.db.clear_conversation(tg)
                        await self.local.report(
                            tg, data.get("month"), callback.message, download=kind == "free_export"
                        )
                    return
                if kind == "disconnect":
                    # Serialize with refresh and record work for this account.
                    async with self.work.locks[tg], self.yt.locks[tg]:
                        self.db.disconnect(tg)
                    await self.show(
                        tg,
                        "YouTrack отключён. Для подключения используй /start.",
                        message=callback.message,
                    )
                    return
                await self.guard(tg)
                if kind == "list":
                    self.db.clear_conversation(tg)
                    await self.list_tasks(tg, message=callback.message, **data)
                elif kind == "card":
                    self.db.clear_conversation(tg)
                    await self.card(tg, data, callback.message)
                elif kind == "search":
                    self.db.set_conversation(tg, "search")
                    await self.show(
                        tg,
                        "<b>🔎 Найти задачу</b>\n\nОтправь номер задачи YouTrack или часть её названия.\n\nНапример: CP-145 или Интеграция MAXMA",
                        self.keys([self.button(tg, "❌ Отмена", "list")]),
                        callback.message,
                    )
                elif kind == "time":
                    issue = await self.yt.issue(tg, data["issue"])
                    require_visible_stage(issue)
                    # The same callback always selects the same operation flow, including double taps.
                    old = self.db.one("SELECT * FROM operations WHERE flow=?", (callback.data,))
                    if old:
                        if old["status"] == "done":
                            await self.success(tg, old, callback.message)
                        else:
                            await self.success(
                                tg, await self.work.reconcile(tg, old["id"]), callback.message
                            )
                        return
                    self.db.set_conversation(tg, "time", data, flow=callback.data)
                    await self.show(
                        tg,
                        f"<b>⏱ Запись времени</b>\n\nЗадача: {escape(issue['idReadable'])} — {escape(issue['summary'], 250)}\n\nСколько времени ты потратил на эту задачу сегодня?\n\n<code>1:30</code> — 1 час 30 минут\n<code>45м</code> — 45 минут\n<code>2ч</code> — 2 часа\n<code>1.5ч</code> — 1 час 30 минут",
                        self.keys([self.button(tg, "❌ Отмена", "card", data)]),
                        callback.message,
                    )
                elif kind == "reconcile":
                    await self.success(
                        tg, await self.work.reconcile(tg, data["op"]), callback.message
                    )
            except (ServiceError, ValueError) as exc:
                await self.failure(tg, exc, callback.message)

    async def text(self, message: Message):
        tg = message.from_user.id
        if not self.s.allowed(tg):
            return
        async with self.locks[tg]:
            try:
                conversation = self.db.conversation(tg)
                if conversation and (
                    conversation["mode"].startswith("free_")
                    or conversation["mode"] == "settings_time"
                ):
                    self.local.user(tg)
                else:
                    await self.guard(tg)
                if not conversation:
                    await self.show(tg, "Выбери задачу командой /tasks.")
                    return
                text = message.text.strip()
                if conversation["mode"] == "settings_time":
                    await self.preferences.save(tg, text)
                elif conversation["mode"] == "free_description":
                    if not 1 <= len(text) <= 1000:
                        raise ValueError("Описание должно содержать от 1 до 1000 символов.")
                    self.db.set_conversation(
                        tg,
                        "free_time",
                        {"description": text, "description_message": message.message_id},
                        flow=conversation["flow"],
                    )
                    await self.local.ask_time(tg, text)
                elif conversation["mode"] == "free_time":
                    if message.message_id == conversation["data"].get("description_message"):
                        return
                    minutes = parse_duration(text, self.s.max_duration_minutes)
                    await self.local.save(tg, conversation, minutes)
                elif conversation["mode"] == "search":
                    if not 2 <= len(text) <= 150:
                        raise ValueError("Введи от 2 до 150 символов для поиска.")
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", text):
                        await self.card(tg, {"issue": text.upper()})
                    else:
                        await self.list_tasks(tg, search=text)
                elif conversation["mode"] == "time":
                    minutes = parse_duration(text, self.s.max_duration_minutes)
                    op = await self.work.record(
                        tg, conversation["flow"], conversation["data"]["issue"], minutes
                    )
                    await self.success(tg, op)
            except InvalidDuration as exc:
                await self.show(
                    tg,
                    escape(str(exc)),
                    self.keys(
                        [
                            self.button(
                                tg,
                                "❌ Отмена",
                                "free_cancel"
                                if conversation["mode"].startswith("free_")
                                else "card",
                                conversation["data"],
                            )
                        ]
                    ),
                )
            except (ServiceError, ValueError) as exc:
                await self.failure(tg, exc)

    async def reminder(self, tg, local: datetime):
        try:
            await self.list_tasks(tg, reminder=local)
        except ServiceError:
            await self.show(
                tg,
                "<b>🔔 Запиши итоги рабочего дня</b>\n\nНе удалось загрузить задачи из YouTrack. Попробуй /tasks позже.",
                self.local.buttons(tg),
            )

    async def setup(self):
        await self.bot.set_my_commands(
            [
                BotCommand(command="start", description="Начало работы"),
                BotCommand(command="tasks", description="Мои задачи"),
                BotCommand(command="free", description="Работа без задачи в YouTrack"),
                BotCommand(command="report", description="Месячный отчёт и CSV"),
                BotCommand(command="teamreport", description="Общий отчёт для администратора"),
                BotCommand(command="settings", description="Время напоминания"),
                BotCommand(command="cancel", description="Отменить ввод"),
                BotCommand(command="account", description="Подключённый аккаунт"),
                BotCommand(command="disconnect", description="Отключить аккаунт"),
            ]
        )

    async def poll(self):
        from aiogram.exceptions import (
            TelegramConflictError,
            TelegramNetworkError,
            TelegramRetryAfter,
        )

        await self.setup()
        while True:
            row = self.db.one("SELECT value FROM meta WHERE key='telegram_offset'")
            offset = int(row["value"]) if row else 0
            try:
                updates = await self.bot.get_updates(
                    offset=offset, timeout=25, allowed_updates=["message", "callback_query"]
                )
            except TelegramConflictError:
                raise RuntimeError("Another Telegram poller or webhook is active") from None
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                continue
            except TelegramNetworkError:
                log.warning("telegram_poll_network_failure")
                await asyncio.sleep(5)
                continue
            for update in updates:
                try:
                    await self.dp.feed_update(self.bot, update)
                except TelegramForbiddenError:
                    event = update.message or update.callback_query
                    if event and event.from_user:
                        self.db.execute(
                            "UPDATE users SET blocked=1 WHERE tg=?", (event.from_user.id,)
                        )
                except Exception as exc:
                    # Do not emit exception strings or local variables (can contain secrets/PII).
                    log.error(
                        "telegram_update_failed update=%s kind=%s",
                        update.update_id,
                        type(exc).__name__,
                    )
                self.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('telegram_offset',?)",
                    (str(update.update_id + 1),),
                )
