"""Personal work log independent of YouTrack tasks and API availability."""

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile

from .duration import human
from .youtrack import Reconnect


def month_dates(month):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError
        start = date.fromisoformat(month + "-01")
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        previous = date(start.year - (start.month == 1), (start.month - 2) % 12 + 1, 1)
        return start.isoformat(), end.isoformat(), previous.strftime("%Y-%m")
    except (ValueError, TypeError):
        raise ValueError("Укажи месяц в формате /report 2026-11.") from None


def export_csv(rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Дата", "Описание", "Минуты", "Время", "Часовой пояс"])
    for row in rows:
        description = row["description"]
        # Prevent spreadsheet formulas when opening a user-authored description.
        if description.lstrip().startswith(("=", "+", "-", "@")) or description.startswith(
            ("\t", "\r", "\n")
        ):
            description = "'" + description
        writer.writerow(
            [row["day"], description, row["minutes"], human(row["minutes"]), row["zone"]]
        )
    return out.getvalue().encode("utf-8-sig")


class FreeTime:
    def __init__(self, ui):
        self.ui = ui
        self.db = ui.db

    def user(self, tg):
        user = self.db.user(tg)
        if not user:
            raise Reconnect()
        # Previously verified Telegram identity remains usable if OAuth has expired.
        return user

    def today(self, tg):
        return datetime.now(ZoneInfo(self.user(tg)["zone"])).date().isoformat()

    def buttons(self, tg):
        return self.ui.keys(
            [self.ui.button(tg, "✍️ Записать работу без YouTrack", "free")],
            [self.ui.report_button(tg)],
        )

    async def begin(self, tg, flow=None, message=None):
        self.user(tg)
        if flow:
            saved = self.db.free_entry(tg, flow)
            if saved:
                return await self.success(tg, saved, message)
        current = self.db.conversation(tg)
        if not (flow and current and current["flow"] == flow):
            self.db.set_conversation(tg, "free_description", flow=flow)
        current = self.db.conversation(tg)
        if current["mode"] == "free_time":
            return await self.ask_time(tg, current["data"]["description"], message)
        await self.ui.show(
            tg,
            "<b>✍️ Работа без задачи в YouTrack</b>\n\nОпиши, что ты делал: например, общался с клиентом, анализировал рекламу или занимался развитием бизнеса.\n\nОт 1 до 1000 символов. Следующим сообщением укажешь время.",
            self.ui.keys([self.ui.button(tg, "❌ Отмена", "free_cancel")]),
            message,
        )

    async def ask_time(self, tg, description, message=None):
        from .telegram import escape

        await self.ui.show(
            tg,
            f"<b>⏱ Сколько времени потратил?</b>\n\n{escape(description, 1000)}\n\nНапример: <code>30м</code>, <code>1:30</code> или <code>2ч</code>.\nЗапись будет сохранена за сегодня по твоему часовому поясу.",
            self.ui.keys([self.ui.button(tg, "❌ Отмена", "free_cancel")]),
            message,
        )

    async def save(self, tg, conversation, minutes):
        user = self.user(tg)
        row = self.db.save_free_entry(
            tg,
            conversation["flow"],
            conversation["data"]["description"],
            minutes,
            self.today(tg),
            user["zone"],
        )
        await self.success(tg, row)

    async def success(self, tg, row, message=None):
        from .telegram import escape

        await self.ui.show(
            tg,
            f"<b>✅ Работа записана</b>\n\n{escape(row['description'], 1000)}\n\nВремя: <b>{human(row['minutes'])}</b>\nДата: {row['day']}\nСохранено в личном журнале бота.",
            self.buttons(tg),
            message,
        )

    async def report(self, tg, month=None, message=None, download=False):
        self.user(tg)
        month = month or self.today(tg)[:7]
        start, end, previous = month_dates(month)
        rows = self.db.free_month(tg, start, end)
        if download:
            if not rows:
                return await self.ui.show(tg, "За этот месяц записей без YouTrack нет.")
            return await self.ui.bot.send_document(
                tg,
                BufferedInputFile(export_csv(rows), filename=f"work-log-{month}.csv"),
                caption=f"Работа вне YouTrack — {month}. Все описания и время.",
            )
        totals = defaultdict(int)
        for row in rows:
            totals[row["day"]] += row["minutes"]
        text = f"<b>📊 Работа вне YouTrack — {month}</b>\n\nВсего: <b>{human(sum(totals.values()))}</b>\nЗаписей: {len(rows)}"
        if rows:
            text += "\n\n" + "\n".join(
                f"{day[8:10]}.{day[5:7]} — {human(minutes)}" for day, minutes in totals.items()
            )
            text += "\n\nОписания всех записей — в CSV."
        else:
            text += "\n\nЗаписей за этот месяц пока нет."
        text += "\n\nЗдесь учитывается только работа без задачи в YouTrack."
        keys = [
            [
                self.ui.button(tg, "⬅️ Предыдущий месяц", "free_report", {"month": previous}),
                self.ui.button(tg, "Следующий месяц ➡️", "free_report", {"month": end[:7]}),
            ],
        ]
        if rows:
            keys.append([self.ui.button(tg, "📥 Скачать CSV", "free_export", {"month": month})])
        keys.append([self.ui.button(tg, "✍️ Записать работу без YouTrack", "free")])
        if self.ui.s.report_admin(tg):
            keys.append(
                [self.ui.button(tg, "📊 Общий отчёт команды", "team_report", {"month": month})]
            )
        await self.ui.show(tg, text, self.ui.keys(*keys), message)
