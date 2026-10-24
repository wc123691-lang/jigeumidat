"""data/iex 의 일별 IEX 종가로 종목별 기술 신호와 '내 신호' 프리셋 점수를 계산한다.

    python compute_signals.py            # 가장 최근 날짜 기준
    python compute_signals.py 20261021   # 특정 날짜 기준

출력 (data/signals/):
- signals_latest.csv     기준일의 종목별 지표와 점수 (+는 매수 쪽, -는 매도 쪽, -100~100).
                         가격 데이터로 언제든 다시 계산할 수 있어 최신 1개만 둔다.
- split_decisions.csv    분할·병합 후보 판정 (확정된 판정은 다음 실행 때 재사용)

액면분할: 하루 사이 가격이 1/2, 1/10처럼 딱 떨어지게 바뀐 날을 후보로 잡고, SEC 공시의 발행 주식 수가
같은 비율로 바뀌었는지로 확인한다. 바이오주처럼 실제로 반토막 나는 경우를 분할로 오인하지 않기 위해서다.
SEC 데이터가 없는 ETF·펀드는 가격 비율만으로 판단한다. SEC 조회에는 SEC_USER_AGENT 환경변수가 필요하다.

가격 자체는 앱에 보여주지 않고, 계산과 알림 판단에만 쓴다.
"""

import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IEX_DIR = ROOT / "data" / "iex"
OUT_DIR = ROOT / "data" / "signals"
DECISIONS_PATH = OUT_DIR / "split_decisions.csv"

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SHARES = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityCommonStockSharesOutstanding.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"  # 위가 비어 있는 회사가 있어 대체용

# 하루 사이 가격 비율이 이 배수(또는 역수)에 1.5% 이내로 맞으면 분할·병합 후보로 본다
SPLIT_FACTORS = (2, 3, 4, 5, 10, 20)
SPLIT_TOLERANCE = 0.015
SHARES_TOLERANCE = 0.15  # 발행 주식 수 비율 허용 오차 (신주 발행 등)
STALE_MINUTES = 30  # 마지막 체결이 마감보다 이만큼 이르면 신뢰도 낮음

# 시안 '내 신호 설정'의 프리셋 비중. 아직 없는 신호(공포탐욕, VIX, 환율, 내부자)는 빠지고
# 남은 신호끼리 비중을 다시 나눈다.
PRESETS = {
    "steady": {"fg": 4, "vix": 2, "fx": 3, "spx": 3, "rsi": 2, "bb": 1, "gap": 3, "ins": 3, "dd": 4},
    "contra": {"fg": 5, "vix": 4, "fx": 2, "spx": 1, "rsi": 4, "bb": 1, "gap": 2, "ins": 4, "dd": 5},
    # 추세 추종은 200일선 괴리를 반대 방향(선 위가 매수 쪽)으로 본다
    "trend": {"fg": 1, "vix": 2, "fx": 1, "spx": 5, "rsi": 2, "bb": 4, "trend": 4, "ins": 2, "dd": 1},
}


def clamp(x: float) -> float:
    return max(-100.0, min(100.0, x))


def minutes_before_close(utc_hms: str, date: str) -> float:
    h, m, s = map(int, utc_hms.split(":"))
    t = h * 60 + m + s / 60
    # 서머타임 기간이면 마감 20:00 UTC, 아니면 21:00 UTC. 마지막 체결이 20:00 이후면 표준시로 본다.
    close = 21 * 60 if t > 20 * 60 else 20 * 60
    return close - t


def load_closes(until: str):
    """symbol -> [(date, price, stale)]"""
    series: dict[str, list] = {}
    files = sorted(p for p in IEX_DIR.glob("iex_????????.csv") if p.stem[4:] <= until)
    for path in files:
        date = path.stem[4:]
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                stale = minutes_before_close(r["last_trade_utc"], date) > STALE_MINUTES
                series.setdefault(r["symbol"], []).append((date, float(r["iex_last_price"]), stale))
    return series, [p.stem[4:] for p in files]


def detect_split(prev: float, cur: float) -> float | None:
    """분할이면 과거 가격에 곱할 비율(예: 10:1 분할 → 0.1)을 돌려준다."""
    ratio = cur / prev
    for k in SPLIT_FACTORS:
        for target in (1 / k, k):
            if abs(ratio / target - 1) <= SPLIT_TOLERANCE:
                return target
    return None


def split_candidates(series: dict) -> list[tuple[str, str, float]]:
    found = []
    for symbol, points in series.items():
        for (_, prev, _), (date, cur, _) in zip(points, points[1:]):
            factor = detect_split(prev, cur)
            if factor:
                found.append((symbol, date, factor))
    return found


class SecUnavailable(Exception):
    """SEC가 접속을 거절했거나 장애. 데이터가 없는 것(404)과 구분한다."""


def sec_get(url: str):
    """JSON을 돌려주고, 데이터가 없으면(404) None."""
    req = urllib.request.Request(url, headers={"User-Agent": os.environ["SEC_USER_AGENT"]})
    time.sleep(0.15)  # SEC 요청 제한: 초당 10회
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SecUnavailable(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise SecUnavailable(str(e.reason)) from e


def share_points(concept: dict | None) -> list[tuple[str, int]]:
    shares = (concept or {}).get("units", {}).get("shares") or []
    return sorted({(x["end"].replace("-", ""), x["val"]) for x in shares})


def judge_split(cik: int | None, date: str, factor: float) -> tuple[str, str]:
    """(판정, 근거). 판정: split(보정) / move(실제 등락, 보정 안 함) / pending(아직 판단 불가, 보정)"""
    if cik is None:
        return "split", "SEC 공시 없음(ETF·펀드 등), 가격 비율로 판단"
    points = share_points(sec_get(SEC_SHARES.format(cik=cik)))
    if not points:
        facts = sec_get(SEC_FACTS.format(cik=cik)) or {}
        points = share_points(facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding"))
    if not points:
        return "split", "SEC 발행 주식 수 데이터 없음, 가격 비율로 판단"
    before = [v for d, v in points if d < date]
    after = [v for d, v in points if d >= date]
    if not before or not after:
        return "pending", "이벤트 이후 공시가 아직 없음, 가격 비율로 임시 보정"
    ratio = after[0] / before[-1]
    if abs(ratio * factor - 1) <= SHARES_TOLERANCE:
        return "split", f"발행 주식 수 x{ratio:.2f}"
    return "move", f"발행 주식 수 x{ratio:.2f}, 분할 아님"


def resolve_splits(candidates: list) -> dict[tuple[str, str], float]:
    """보정할 (symbol, date) -> 가격 배율. 확정된 판정은 파일에 남겨 다음에 재사용한다."""
    saved = {}
    if DECISIONS_PATH.exists():
        with DECISIONS_PATH.open(encoding="utf-8") as f:
            saved = {(r["symbol"], r["date"]): r for r in csv.DictReader(f)}
    ciks = None
    sec_error = None
    rows = []
    for symbol, date, factor in sorted(candidates, key=lambda c: (c[1], c[0])):
        row = saved.get((symbol, date))
        if row is None or row["decision"] == "pending":
            if sec_error is None:
                try:
                    if ciks is None:
                        ciks = {v["ticker"]: v["cik_str"] for v in sec_get(SEC_TICKERS).values()}
                    decision, reason = judge_split(ciks.get(symbol.replace(".", "-")), date, factor)
                except SecUnavailable as e:
                    sec_error = e
                    print(f"SEC 조회 실패({e}), 남은 후보는 가격 비율로 임시 보정", file=sys.stderr)
            if sec_error is not None:
                decision, reason = "pending", f"SEC 조회 실패({sec_error}), 가격 비율로 임시 보정"
            row = {"symbol": symbol, "date": date, "price_factor": factor, "decision": decision, "reason": reason}
        rows.append(row)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with DECISIONS_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "date", "price_factor", "decision", "reason"])
        w.writeheader()
        w.writerows(rows)
    return {(r["symbol"], r["date"]): float(r["price_factor"]) for r in rows if r["decision"] != "move"}


def adjusted_prices(symbol: str, points: list, splits: dict) -> list[float]:
    prices = [p for _, p, _ in points]
    for i in range(len(prices) - 1, 0, -1):
        factor = splits.get((symbol, points[i][0]))
        if factor:
            for j in range(i):
                prices[j] *= factor
    return prices


def rsi(prices: list[float], period: int = 14) -> float | None:
    if len(prices) <= period:
        return None
    changes = [b - a for a, b in zip(prices, prices[1:])]
    gain = sum(max(c, 0) for c in changes[:period]) / period
    loss = sum(max(-c, 0) for c in changes[:period]) / period
    for c in changes[period:]:  # Wilder 평활
        gain = (gain * (period - 1) + max(c, 0)) / period
        loss = (loss * (period - 1) + max(-c, 0)) / period
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def bollinger(prices: list[float], period: int = 20):
    """(%B, 밴드폭, 밴드폭이 최근 120일 중 하위 10%인지)"""
    if len(prices) < period:
        return None, None, None
    widths = []
    for end in range(max(period, len(prices) - 120), len(prices) + 1):
        window = prices[end - period : end]
        mid, sd = statistics.fmean(window), statistics.pstdev(window)
        widths.append((4 * sd / mid if mid else 0, mid, sd))
    width, mid, sd = widths[-1]
    pct_b = 0.5 if sd == 0 else (prices[-1] - (mid - 2 * sd)) / (4 * sd)
    squeeze = len(widths) >= 60 and width <= sorted(w for w, _, _ in widths)[len(widths) // 10]
    return pct_b, width, squeeze


def scores(prices: list[float]) -> dict:
    out = {}
    r = rsi(prices)
    if r is not None:
        out["rsi14"] = r
        out["s_rsi"] = clamp((50 - r) * 100 / 30)  # 20 이하 +100, 80 이상 -100
    pct_b, width, squeeze = bollinger(prices)
    if pct_b is not None:
        out.update(bb_pctb=pct_b, bb_width=width, bb_squeeze=squeeze)
        out["s_bb"] = clamp((0.5 - pct_b) * 200)  # 하단 밴드 +100, 상단 밴드 -100
    if len(prices) >= 200:
        gap = prices[-1] / statistics.fmean(prices[-200:]) - 1
        out["sma200_gap"] = gap
        out["s_gap"] = clamp(-gap * 500)  # 200일선 -20% 이하 +100, +20% 이상 -100
        out["s_trend"] = -out["s_gap"]
    if len(prices) >= 200:
        dd = prices[-1] / max(prices[-252:]) - 1
        out["dd52w"] = dd
        out["s_dd"] = clamp((-dd - 0.10) * 100 / 0.30)  # 고점 -40% +100, -10% 0, 고점 근처 -33
    return out


def preset_score(signal: dict, weights: dict) -> float | None:
    used = {k: w for k, w in weights.items() if f"s_{k}" in signal and w > 0}
    if not used:
        return None
    return sum(signal[f"s_{k}"] * w for k, w in used.items()) / sum(used.values())


def main() -> None:
    until = sys.argv[1] if len(sys.argv) > 1 else "99999999"
    series, dates = load_closes(until)
    date = dates[-1]

    splits = resolve_splits(split_candidates(series))
    market = {}
    spy = series.get("SPY")
    if spy:
        spy_prices = adjusted_prices("SPY", spy, splits)
        if len(spy_prices) >= 200:
            spy_gap = spy_prices[-1] / statistics.fmean(spy_prices[-200:]) - 1
            market["s_spx"] = clamp(spy_gap * 500)  # 시장이 200일선 위면 매수 쪽 (추세)

    rows = []
    for symbol, points in sorted(series.items()):
        if points[-1][0] != date:
            continue  # 기준일에 체결이 없던 종목
        prices = adjusted_prices(symbol, points, splits)
        signal = {**market, **scores(prices)}
        row = {
            "date": date,
            "symbol": symbol,
            "days": len(points),
            "coverage": round(len(points) / len(dates), 3),
            "stale_today": points[-1][2],
        }
        for key in ("rsi14", "bb_pctb", "bb_width", "bb_squeeze", "sma200_gap", "dd52w",
                    "s_rsi", "s_bb", "s_gap", "s_trend", "s_dd", "s_spx"):
            value = signal.get(key)
            row[key] = round(value, 4) if isinstance(value, float) else value
        for name, weights in PRESETS.items():
            value = preset_score(signal, weights)
            row[f"score_{name}"] = round(value, 1) if value is not None else None
        rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "signals_latest.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"기준일 {date} ({len(dates)}일치) · 종목 {len(rows):,}개 → {out}")
    print(f"분할·병합으로 보정 {len(splits)}건 → {DECISIONS_PATH}")


if __name__ == "__main__":
    main()
