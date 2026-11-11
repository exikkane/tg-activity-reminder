"""Personal reminder settings independent of the YouTrack API."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class Preferences:
    def __init__(self, ui):
        self.ui = ui

    def now(self, tg):
        return datetime.now(ZoneInfo(self.ui.local.user(tg)["zone"]))

    async def show(self, tg, message=None, notice=""):
        from .telegram import escape

        user = self.ui.local.user(tg)
        preference = self.ui.db.reminder_preference(tg)
        value = preference["time"] if preference else self.ui.s.reminder_time
        self.ui.db.clear_conversation(tg)
        days = (
            "Каждый день, включая выходные"
            if self.ui.s.reminder_days == "all"
            else self.ui.s.reminder_days
        )
        text = f"<b>🔔 Время напоминания</b>\n\nТвоё время: <b>{value}</b>\nЧасовой пояс: {escape(user['zone'])}\nДни: {escape(days)}\n\n{notice or 'Выбери удобное время — настройка изменится только для тебя.'}"
        if not user["active"]:
            text += "\n\nДля получения напоминаний обнови подключение через /start."
        await self.ui.show(
            tg,
            text,
            self.ui.keys(
                [self.ui.button(tg, "🕒 Изменить время", "settings_time")],
                [self.ui.button(tg, "📋 Мои задачи", "list")],
            ),
            message,
        )

    async def begin(self, tg, message=None):
        from .telegram import escape

        user = self.ui.local.user(tg)
        self.ui.db.set_conversation(tg, "settings_time")
        await self.ui.show(
            tg,
            f"Введи время в формате <b>ЧЧ:ММ</b>, например <code>19:30</code>.\nЧасовой пояс: {escape(user['zone'])}.\n\nЕсли это время сегодня уже прошло или напоминание уже отправлено, новое расписание начнёт действовать завтра.",
            self.ui.keys([self.ui.button(tg, "❌ Отмена", "settings")]),
            message,
        )

    async def save(self, tg, value):
        self.ui.local.user(tg)
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Укажи время от 00:00 до 23:59 в формате ЧЧ:ММ, например 19:30.")
        local = self.now(tg)
        day = local.date()
        sent = self.ui.db.one(
            "SELECT 1 FROM notifications WHERE tg=? AND day=?", (tg, day.isoformat())
        )
        if value <= local.strftime("%H:%M") or sent:
            day += timedelta(days=1)
        self.ui.db.save_reminder_preference(tg, value, day.isoformat())
        await self.show(
            tg,
            notice=f"✅ Сохранено. Новое время действует с {day.strftime('%d.%m.%Y')}. Не более одного напоминания в день.",
        )
