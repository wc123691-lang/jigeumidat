"""시장 전체 신호: 자체 공포·탐욕 지수, 변동성, 원/달러 환율, 장단기 금리차.

공포·탐욕 지수는 IEX 종가만으로 만든다 (VIX·ICE 하이일드 지수는 저작권·재배포 제한이 있어 쓰지 않음).
구성요소 6개를 각각 보유 기간 전체에서의 백분위(0~100)로 바꿔 평균한다. 0 = 극단적 공포, 100 = 극단적 탐욕.
데이터가 쌓일수록(또는 과거 IEX 데이터를 더 받을수록) 백분위 기준이 안정된다.

FRED(키 불필요 CSV):
- T10Y2Y  장단기 금리차 (미 재무부 데이터 기반)
- DEXKOUS 원/달러 환율 (연준 H.10, 약 1주 늦게 갱신)
"""

import csv
import io
import math
import statistics
import sys
import urllib.error
import urllib.request

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"

MIN_PRICE = 5.0  # 시장 폭·신고가 계산에서 동전주 제외
MIN_COVERAGE = 0.9


def fred(series_id: str) -> dict[str, float]:
    """YYYYMMDD -> 값. 실패하면 빈 dict (시장 신호 일부만 빠지고 계속 진행)."""
    try:
        text = urllib.request.urlopen(FRED_CSV.format(id=series_id), timeout=60).read().decode()
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"FRED {series_id} 조회 실패({e}), 건너뜀", file=sys.stderr)
        return {}
    out = {}
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) == 2 and row[1] not in ("", "."):
            out[row[0].replace("-", "")] = float(row[1])
    return out


def ret(prices: dict, dates: list, i: int, n: int) -> float | None:
    a, b = prices.get(dates[i - n]) if i >= n else None, prices.get(dates[i])
    return b / a - 1 if a and b else None


def percentile(values: list[float], x: float) -> float:
    return 100 * sum(v <= x for v in values) / len(values)


def components(dates: list[str], adjusted: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """구성요소별 날짜 -> 원값 (클수록 탐욕)."""
    spy, tlt, hyg, ief = (adjusted.get(s, {}) for s in ("SPY", "TLT", "HYG", "IEF"))
    universe = [
        p for p in adjusted.values()
        if len(p) >= MIN_COVERAGE * len(dates) and p.get(dates[-1], 0) >= MIN_PRICE
    ]
    comp = {k: {} for k in ("momentum", "strength", "breadth", "volatility", "safe_haven", "junk")}
    for i, d in enumerate(dates):
        if i >= 125 and d in spy:
            window = [spy[x] for x in dates[i - 124 : i + 1] if x in spy]
            comp["momentum"][d] = spy[d] / statistics.fmean(window) - 1
        if i >= 63:
            highs = lows = above = counted = 0
            for p in universe:
                if d not in p:
                    continue
                past = [p[x] for x in dates[i - 63 : i] if x in p]
                if len(past) < 50:
                    continue
                highs += p[d] >= max(past)
                lows += p[d] <= min(past)
                above += p[d] > statistics.fmean(past[-50:])
                counted += 1
            if counted:
                comp["strength"][d] = (highs - lows) / counted
                comp["breadth"][d] = above / counted
        if i >= 20 and d in spy:
            rets = [math.log(spy[dates[j]] / spy[dates[j - 1]]) for j in range(i - 19, i + 1)
                    if dates[j] in spy and dates[j - 1] in spy]
            if len(rets) >= 15:
                comp["volatility"][d] = -statistics.pstdev(rets) * math.sqrt(252)  # 낮을수록 탐욕 → 부호 반전
        s, t = ret(spy, dates, i, 20), ret(tlt, dates, i, 20)
        if s is not None and t is not None:
            comp["safe_haven"][d] = s - t
        h, f = ret(hyg, dates, i, 20), ret(ief, dates, i, 20)
        if h is not None and f is not None:
            comp["junk"][d] = h - f
    return comp


def fear_greed_history(comp: dict) -> list[dict]:
    """날짜별 지수와 구성요소 백분위. 구성요소가 4개 이상 있는 날만."""
    history = []
    all_dates = sorted({d for c in comp.values() for d in c})
    for d in all_dates:
        row = {"date": d}
        for name, series in comp.items():
            if d in series:
                row[name] = round(percentile(list(series.values()), series[d]), 1)
        parts = [row[k] for k in comp if k in row]
        if len(parts) >= 4:
            row["fear_greed"] = round(statistics.fmean(parts), 1)
            history.append(row)
    return history


def fear_greed_label(v: float) -> str:
    return "극단적 공포" if v < 25 else "공포" if v < 45 else "중립" if v <= 55 else "탐욕" if v <= 75 else "극단적 탐욕"


def clamp(x: float) -> float:
    return max(-100.0, min(100.0, x))


def market_signals(dates: list[str], adjusted: dict[str, dict[str, float]]) -> tuple[dict, list[dict]]:
    """(최신 시장 신호, 공포·탐욕 지수 이력). 신호 점수는 +가 매수 쪽."""
    comp = components(dates, adjusted)
    history = fear_greed_history(comp)
    latest = {"date": dates[-1]}
    if history:
        fg = history[-1]
        latest.update(fear_greed=fg["fear_greed"], fear_greed_label=fear_greed_label(fg["fear_greed"]),
                      fear_greed_days=len(history))
        latest["s_fg"] = clamp((50 - fg["fear_greed"]) * 2)  # 공포일수록 매수 쪽
        if "volatility" in fg:
            latest["volatility_pct"] = 100 - fg["volatility"]  # 변동성이 과거 대비 높은 정도
            latest["s_vix"] = clamp((latest["volatility_pct"] - 50) * 2)

    spread = fred("T10Y2Y")
    if spread:
        d = max(spread)
        latest.update(t10y2y=spread[d], t10y2y_date=d)

    fx = fred("DEXKOUS")
    if fx:
        d = max(fx)
        year = [v for k, v in fx.items() if k > str(int(d[:4]) - 1) + d[4:]]
        avg = statistics.fmean(year)
        latest.update(usdkrw=fx[d], usdkrw_date=d, usdkrw_1y_avg=round(avg, 2))
        latest["s_fx"] = clamp(-(fx[d] / avg - 1) * 1000)  # 1년 평균보다 10% 싸면 +100

    spy = adjusted.get("SPY", {})
    window = [spy[d] for d in dates[-200:] if d in spy]
    if len(window) >= 180 and dates[-1] in spy:
        latest["spy_sma200_gap"] = spy[dates[-1]] / statistics.fmean(window) - 1
        latest["s_spx"] = clamp(latest["spy_sma200_gap"] * 500)  # 시장이 200일선 위면 매수 쪽 (추세)
    return latest, history
