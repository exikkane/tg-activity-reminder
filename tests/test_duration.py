import pytest

from bot.duration import InvalidDuration, parse_duration


@pytest.mark.parametrize(
    "text,minutes",
    [
        ("1:30", 90),
        ("0:45", 45),
        ("2:00", 120),
        ("1ч 30м", 90),
        ("1ч30м", 90),
        ("1h 30m", 90),
        ("90м", 90),
        ("90m", 90),
        ("2ч", 120),
        ("2h", 120),
        ("1.5ч", 90),
        ("1,5ч", 90),
        ("45 минут", 45),
        ("2 часа", 120),
        ("30м", 30),
        ("1ч", 60),
        (" 1 Ч 30 М ", 90),
        ("0.5h", 30),
    ],
)
def test_formats(text, minutes):
    assert parse_duration(text) == minutes


@pytest.mark.parametrize(
    "text",
    [
        "",
        "45",
        "-1ч",
        "+1ч",
        "0м",
        "1:60",
        "1:3",
        "1ч -30м",
        "2 часа junk",
        "NaNh",
        "1e3h",
        "1h1h",
        "0.001h",
        "100ч",
        "9" * 200 + "h",
    ],
)
def test_invalid(text):
    with pytest.raises(InvalidDuration):
        parse_duration(text)


def test_limit():
    assert parse_duration("16h") == 960
    with pytest.raises(InvalidDuration):
        parse_duration("16h1m")
