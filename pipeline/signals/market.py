"""시장 전체 신호: 자체 공포·탐욕 지수, 변동성, 원/달러 환율, 장단기 금리차.

공포·탐욕 지수는 IEX 종가만으로 만든다 (VIX·ICE 하이일드 지수는 저작권·재배포 제한이 있어 쓰지 않음).
구성요소 6개(클수록 탐욕)의 날짜별 원값을 fg_components.csv에 쌓아 두고 새 날짜만 계산한다.
각 구성요소를 전체 이력 중 백분위(0~100)로 바꿔 평균한 것이 지수다. 0 = 극단적 공포, 100 = 극단적 탐욕.
시장 폭·신고가 계산은 종목 수가 많아 분할 보정을 하지 않는다 (분할 종목은 전체의 극히 일부).

FRED(키 불필요 CSV): T10Y2Y 장단기 금리차. 환율은 ECB 기준환율(키 불필요, 매일 갱신)로 원/달러를 구한다.
"""

import bisect
import csv
import io
import math
import statistics
import sys
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
ECB_CSV = ("https://data-api.ecb.europa.eu/service/data/EXR/D.KRW+USD.EUR.SP00.A"
           "?format=csvdata&startPeriod={start}")

COMPONENTS = ("momentum", "strength", "breadth", "volatility", "safe_haven", "junk")
ETFS = ("SPY", "TLT", "HYG", "IEF")
MIN_PRICE = 5.0  # 시장 폭·신고가 계산에서 동전주 제외
HIGH_LOW_DAYS = 63
SMA_DAYS = 50


def clamp(x: float) -> float:
    return max(-100.0, min(100.0, x))


def fetch_csv(url: str, label: str) -> list[dict]:
    """실패하면 빈 목록 (시장 신호 일부만 빠지고 계속 진행)."""
    try:
        text = urllib.request.urlopen(url, timeout=60).read().decode()
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"{label} 조회 실패({e}), 건너뜀", file=sys.stderr)
        return []
    return list(csv.DictReader(io.StringIO(text)))


def read_day(path: Path) -> dict[str, float]:
    with path.open(encoding="utf-8") as f:
        return {r["symbol"]: float(r["iex_last_price"]) for r in csv.DictReader(f)}


def day_components(i: int, dates: list, today: dict, etf: dict, highs_lows: dict, smas: dict) -> dict:
    """i번째 날의 구성요소 원값. highs_lows·smas는 종목별 과거 종가 창(오늘 제외)."""
    row = {}
    spy = etf["SPY"]
    d = dates[i]
    if d in spy:
        window = [spy[x] for x in dates[max(0, i - 124) : i + 1] if x in spy]
        if i >= 124 and len(window) >= 110:
            row["momentum"] = spy[d] / statistics.fmean(window) - 1
        rets = [math.log(spy[dates[j]] / spy[dates[j - 1]]) for j in range(max(1, i - 19), i + 1)
                if dates[j] in spy and dates[j - 1] in spy]
        if len(rets) >= 15:
            row["volatility"] = -statistics.pstdev(rets) * math.sqrt(252)  # 변동성 낮을수록 탐욕

    highs = lows = above = counted = 0
    for symbol, price in today.items():
        past = highs_lows.get(symbol)
        if price < MIN_PRICE or past is None or len(past) < 50:
            continue
        highs += price >= max(past)
        lows += price <= min(past)
        sma = smas[symbol]
        above += price > sum(sma) / len(sma)
        counted += 1
    if counted >= 100:
        row["strength"] = (highs - lows) / counted
        row["breadth"] = above / counted

    def ret20(series):
        if i < 20:
            return None
        a, b = series.get(dates[i - 20]), series.get(d)
        return b / a - 1 if a and b else None

    s, t, h, f = (ret20(etf[x]) for x in ETFS)
    if s is not None and t is not None:
        row["safe_haven"] = s - t
    if h is not None and f is not None:
        row["junk"] = h - f
    return row


def update_components(iex_dir: Path, path: Path) -> dict[str, dict[str, float]]:
    """fg_components.csv를 새 날짜까지 채우고 date -> {구성요소: 값}을 돌려준다."""
    files = sorted(iex_dir.glob("iex_????????.csv"))
    dates = [p.stem[4:] for p in files]
    saved: dict[str, dict[str, float]] = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                saved[r["date"]] = {k: float(r[k]) for k in COMPONENTS if r.get(k)}
    missing = [i for i, d in enumerate(dates) if d not in saved]
    if missing:
        # 과거 날짜가 새로 채워졌으면 그 뒤 날짜들도 창이 달라지므로 모두 다시 계산한다
        todo = set(range(missing[0], len(dates)))
        start = max(0, missing[0] - 130)  # 창(63일 고저, 125일 평균)을 채우기 위한 준비 구간
        print(f"공포·탐욕 구성요소 계산: {len(todo)}일 (파일 {len(dates) - start}개 읽음)")
        etf = {x: {} for x in ETFS}
        highs_lows: dict[str, deque] = {}
        smas: dict[str, deque] = {}
        for i in range(start, len(dates)):
            today = read_day(files[i])
            for x in ETFS:
                if x in today:
                    etf[x][dates[i]] = today[x]
            if i in todo:
                saved[dates[i]] = day_components(i, dates, today, etf, highs_lows, smas)
            for symbol, price in today.items():
                highs_lows.setdefault(symbol, deque(maxlen=HIGH_LOW_DAYS)).append(price)
                smas.setdefault(symbol, deque(maxlen=SMA_DAYS)).append(price)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", *COMPONENTS])
            for d in sorted(saved):
                w.writerow([d, *(round(saved[d][k], 6) if k in saved[d] else "" for k in COMPONENTS)])
    return saved


def fear_greed_history(comp_by_date: dict) -> list[dict]:
    """날짜별 지수와 구성요소 백분위 (전체 이력 기준). 구성요소가 4개 이상 있는 날만."""
    ranked = {k: sorted(v[k] for v in comp_by_date.values() if k in v) for k in COMPONENTS}
    history = []
    for d in sorted(comp_by_date):
        row = {"date": d}
        for k, value in comp_by_date[d].items():
            row[k] = round(100 * bisect.bisect_right(ranked[k], value) / len(ranked[k]), 1)
        parts = [row[k] for k in COMPONENTS if k in row]
        if len(parts) >= 4:
            row["fear_greed"] = round(statistics.fmean(parts), 1)
            history.append(row)
    return history


def fear_greed_label(v: float) -> str:
    return "극단적 공포" if v < 25 else "공포" if v < 45 else "중립" if v <= 55 else "탐욕" if v <= 75 else "극단적 탐욕"


def usd_krw(start: str) -> dict[str, float]:
    """ECB 기준환율로 계산한 원/달러 (KRW/EUR ÷ USD/EUR). YYYYMMDD -> 값."""
    per_eur = {"KRW": {}, "USD": {}}
    for r in fetch_csv(ECB_CSV.format(start=start), "ECB 환율"):
        if r.get("OBS_VALUE"):
            per_eur[r["CURRENCY"]][r["TIME_PERIOD"].replace("-", "")] = float(r["OBS_VALUE"])
    return {d: per_eur["KRW"][d] / per_eur["USD"][d] for d in per_eur["KRW"] if d in per_eur["USD"]}


def market_signals(iex_dir: Path, out_dir: Path, spy_recent: list[float]) -> tuple[dict, list[dict]]:
    """(최신 시장 신호, 공포·탐욕 지수 이력). 점수는 +가 매수 쪽. spy_recent는 분할 보정된 최근 SPY 종가."""
    components = update_components(iex_dir, out_dir / "fg_components.csv")
    history = fear_greed_history(components)
    latest: dict = {}
    if history:
        fg = history[-1]
        latest.update(date=fg["date"], fear_greed=fg["fear_greed"], fear_greed_label=fear_greed_label(fg["fear_greed"]),
                      fear_greed_days=len(history))
        # 홈 화면 카드용 원값 (백분위가 아닌 실제 값)
        raw = components[fg["date"]]
        if "breadth" in raw:
            latest["breadth_above_sma50"] = raw["breadth"]  # 50일선 위 종목 비율
        if "volatility" in raw:
            latest["spy_volatility_20d"] = -raw["volatility"]  # 연율화 변동성 (저장은 부호 반전)
            vols = sorted(-c["volatility"] for c in components.values() if "volatility" in c)
            latest["spy_volatility_median"] = statistics.median(vols)
        latest["s_fg"] = clamp((50 - fg["fear_greed"]) * 2)  # 공포일수록 매수 쪽
        if "volatility" in fg:
            latest["volatility_pct"] = 100 - fg["volatility"]  # 변동성이 과거 대비 높은 정도
            latest["s_vix"] = clamp((latest["volatility_pct"] - 50) * 2)

    spread = {r["observation_date"].replace("-", ""): float(r["T10Y2Y"])
              for r in fetch_csv(FRED_CSV.format(id="T10Y2Y"), "FRED T10Y2Y") if r.get("T10Y2Y") not in (None, "", ".")}
    if spread:
        d = max(spread)
        latest.update(t10y2y=spread[d], t10y2y_date=d)

    last = latest.get("date") or "99999999"
    fx = usd_krw(f"{int(last[:4]) - 1}-{last[4:6]}-{last[6:]}")
    if fx:
        d = max(fx)
        avg = statistics.fmean(fx.values())
        latest.update(usdkrw=round(fx[d], 2), usdkrw_date=d, usdkrw_1y_avg=round(avg, 2))
        latest["s_fx"] = clamp(-(fx[d] / avg - 1) * 1000)  # 1년 평균보다 10% 싸면 +100

    if len(spy_recent) >= 200:
        latest["spy_sma200_gap"] = spy_recent[-1] / statistics.fmean(spy_recent[-200:]) - 1
        latest["s_spx"] = clamp(latest["spy_sma200_gap"] * 500)  # 시장이 200일선 위면 매수 쪽 (추세)
    return latest, history
