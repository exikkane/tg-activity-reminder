import re
from decimal import Decimal


class InvalidDuration(ValueError):
    pass


def parse_duration(raw: str, maximum: int = 960) -> int:
    text = raw.strip().lower().replace(",", ".")
    if not text or len(text) > 100:
        raise InvalidDuration("Укажи время с единицами: 45м, 2ч или 1:30.")
    clock = re.fullmatch(r"(\d{1,3}):(\d{2})", text)
    if clock:
        hours, minutes = map(int, clock.groups())
        if minutes >= 60:
            raise InvalidDuration("После двоеточия должно быть от 00 до 59 минут.")
        result = Decimal(hours * 60 + minutes)
    else:
        pattern = r"(\d+(?:\.\d+)?)\s*(часов|часа|час|ч|hours|hour|h|минуты|минуту|минут|минута|мин|м|minutes|minute|m)"
        matches = list(re.finditer(pattern, text))
        if not matches or re.sub(pattern, "", text).strip():
            raise InvalidDuration("Не удалось понять время. Примеры: 45м, 2ч, 1ч 30м, 1:30.")
        result = Decimal(0)
        seen = set()
        for match in matches:
            number, unit = match.groups()
            hour = unit.startswith(("ч", "h"))
            if hour in seen:
                raise InvalidDuration("Укажи часы и минуты по одному разу.")
            seen.add(hour)
            result += Decimal(number) * (60 if hour else 1)
    if result != result.to_integral_value():
        raise InvalidDuration("Время должно составлять целое число минут.")
    if not 0 < result <= maximum:
        raise InvalidDuration(f"Укажи от 1 до {maximum} минут за одну запись.")
    return int(result)


def human(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return " ".join(filter(None, [f"{h} ч" if h else "", f"{m} мин" if m or not h else ""]))
