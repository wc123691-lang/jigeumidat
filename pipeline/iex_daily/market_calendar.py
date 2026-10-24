"""미국 증시(NYSE) 정규 휴장일 계산 (외부 라이브러리 없이).

일일 수집이 휴장일 다음 날 새 파일을 몇 시간씩 기다리지 않도록 쓴다.
정규 휴장일만 계산한다. 국장일 같은 임시 휴장(2018-12-05, 2025-01-09 등)은 모르므로
그런 날은 예전처럼 기다렸다가 끝난다.
"""

from datetime import date, timedelta


def _easter(year: int) -> date:
    """그레고리력 부활절 (익명의 그레고리력 알고리즘)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그달의 n번째 요일 (n=-1이면 마지막). weekday: 월=0."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """토요일 휴일은 금요일에, 일요일 휴일은 월요일에 쉰다."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> set[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),   # 마틴 루서 킹 데이
        _nth_weekday(year, 2, 0, 3),   # 대통령의 날
        _easter(year) - timedelta(days=2),  # 성금요일
        _nth_weekday(year, 5, 0, -1),  # 메모리얼 데이
        _observed(date(year, 7, 4)),   # 독립기념일
        _nth_weekday(year, 9, 0, 1),   # 노동절
        _nth_weekday(year, 11, 3, 4),  # 추수감사절
        _observed(date(year, 12, 25)),  # 성탄절
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # 새해가 토요일이면 전날(12/31)에 쉬지 않는다 (NYSE 규칙)
        days.add(_observed(new_year))
    if year >= 2022:  # 준틴스는 2022년부터 증시 휴일
        days.add(_observed(date(year, 6, 19)))
    return days


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
