"""앱용 최근 1년 종가(분할 보정, IEX 종가 기준)와 전체 기간 시장 신호 이력.

- 종가: 내부자 화면 차트와 내 신호 시뮬레이션(전체 기간 파일 export_history.py에 이어 붙임)에 쓴다. 전 종목.
  compute_signals.py 다음에 실행해 분할 판정(split_decisions.csv + PC에서 확인한 split_decisions_full.csv)을 쓴다 (SEC 조회 없음).
- 시장 신호 이력: 내 신호 시뮬레이션의 시장 쪽 점수(공포·탐욕, 변동성, 환율, S&P500 200일선).
  market.py가 최신 하루에 쓰는 공식을 날짜마다 그대로 적용한다.

매일 모든 파일이 바뀌어 main 브랜치에 올리면 저장소가 계속 커지므로, 워크플로가 결과를
기록이 남지 않는 'prices' 브랜치에 덮어써 올린다 (.github/workflows/iex_daily.yml).

    python export_prices.py [출력 폴더]   # 기본 output/prices

출력: _dates.json  ["YYYY-MM-DD", ...]  (최근 1년 공통 거래일)
      <티커>.json   [종가 또는 null, ...] (_dates.json과 같은 순서, 그날 IEX 체결이 없으면 null)
      CON 같은 윈도우 예약 이름은 _CON.json (pipeline/insider/history.py와 같은 규칙)
      market.json  {"dates": [전체 거래일], "fg", "vix", "fx", "spx": [점수 또는 null, ...]}
"""

import bisect
import csv
import json
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

from compute_signals import IEX_DIR, OUT_DIR, adjusted_prices, applies, clamp, load_closes
from price_history import load_decisions
from market import usd_krw

ROOT = Path(__file__).resolve().parents[2]
DAYS = 252  # 1년
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM\d|LPT\d)$")


def file_name(ticker: str) -> str:
    return ("_" if RESERVED.match(ticker) else "") + ticker + ".json"


def spy_closes(files: list[Path]) -> list[float | None]:
    """날짜별 SPY 종가 (SPY는 분할이 없다). 파일이 종목 이름순이라 SPY 줄만 찾아 읽는다."""
    out = []
    for path in files:
        price = None
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("SPY,"):
                    price = float(line.split(",")[1])
                    break
        out.append(price)
    return out


def market_history() -> dict:
    files = sorted(IEX_DIR.glob("iex_????????.csv"))
    dates = [p.stem[4:] for p in files]
    with (OUT_DIR / "fear_greed_history.csv").open(encoding="utf-8") as f:
        fg = {r["date"]: r for r in csv.DictReader(f)}
    first = date(int(dates[0][:4]) - 1, int(dates[0][4:6]), int(dates[0][6:]))
    fx = usd_krw(first.isoformat())
    fx_dates = sorted(fx)
    spy = spy_closes(files)
    out = {"dates": [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates], "fg": [], "vix": [], "fx": [], "spx": []}
    spy_seen: list[float] = []
    for d, price in zip(dates, spy):
        row = fg.get(d)
        out["fg"].append(round(clamp((50 - float(row["fear_greed"])) * 2), 1) if row else None)
        vol = row.get("volatility") if row else None
        out["vix"].append(round(clamp(((100 - float(vol)) - 50) * 2), 1) if vol else None)
        # 그날까지 1년 동안의 평균 환율 대비 (market.py: 1년 평균보다 10% 싸면 +100)
        hi = bisect.bisect_right(fx_dates, d)
        year_ago = (date(int(d[:4]), int(d[4:6]), int(d[6:])) - timedelta(days=365)).strftime("%Y%m%d")
        window = [fx[k] for k in fx_dates[bisect.bisect_right(fx_dates, year_ago):hi]]
        out["fx"].append(round(clamp(-(window[-1] / statistics.fmean(window) - 1) * 1000), 1) if window else None)
        if price is not None:
            spy_seen.append(price)
        if price is not None and len(spy_seen) >= 200:
            gap = spy_seen[-1] / statistics.fmean(spy_seen[-200:]) - 1
            out["spx"].append(round(clamp(gap * 500), 1))
        else:
            out["spx"].append(None)
    return out


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "prices"
    out.mkdir(parents=True, exist_ok=True)
    series, dates = load_closes("99999999")
    dates = dates[-DAYS:]
    # 분할 판정: PC에서 SEC로 확인한 전체 기간 판정 + 오늘 신호 계산의 판정
    splits = {key: float(r["price_factor"]) for key, r in load_decisions().items() if applies(r)}

    index = {d: i for i, d in enumerate(dates)}
    written = 0
    for ticker in sorted(series):
        points = series[ticker]
        prices = adjusted_prices(ticker, points, splits)
        row: list[float | None] = [None] * len(dates)
        for (d, _, _), p in zip(points, prices):
            if d in index:
                # 유효숫자 5자리 정도면 차트에 충분하다 (파일 크기 절약)
                row[index[d]] = float(f"{p:.5g}")
        if any(v is not None for v in row):
            (out / file_name(ticker)).write_text(json.dumps(row, separators=(",", ":")), encoding="utf-8")
            written += 1
    (out / "_dates.json").write_text(json.dumps([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]), encoding="utf-8")
    market = market_history()
    (out / "market.json").write_text(json.dumps(market, separators=(",", ":")), encoding="utf-8")
    print(f"종가 {written}개 종목, {dates[0]}~{dates[-1]} · 시장 신호 이력 {len(market['dates'])}일 → {out}")


if __name__ == "__main__":
    main()
