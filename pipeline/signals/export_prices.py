"""앱의 내부자 거래 화면 차트용 최근 1년 종가 (분할 보정, IEX 종가 기준).

내부자 거래 내역이 있는 종목(data/insider/by_ticker)만 내보낸다. compute_signals.py 다음에 실행해
그 실행이 남긴 분할 판정(split_decisions.csv)을 그대로 쓴다 (SEC 조회 없음).

매일 모든 파일이 바뀌어 main 브랜치에 올리면 저장소가 계속 커지므로, 워크플로가 결과를
기록이 남지 않는 'prices' 브랜치에 덮어써 올린다 (.github/workflows/iex_daily.yml).

    python export_prices.py [출력 폴더]   # 기본 output/prices

출력: _dates.json  ["YYYY-MM-DD", ...]  (모든 종목 공통 거래일)
      <티커>.json   [종가 또는 null, ...] (_dates.json과 같은 순서, 그날 IEX 체결이 없으면 null)
      CON 같은 윈도우 예약 이름은 _CON.json (pipeline/insider/history.py와 같은 규칙)
"""

import csv
import json
import re
import sys
from pathlib import Path

from compute_signals import DECISIONS_PATH, adjusted_prices, load_closes

ROOT = Path(__file__).resolve().parents[2]
BY_TICKER = ROOT / "data" / "insider" / "by_ticker"
DAYS = 252  # 1년
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM\d|LPT\d)$")


def file_name(ticker: str) -> str:
    return ("_" if RESERVED.match(ticker) else "") + ticker + ".json"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "prices"
    out.mkdir(parents=True, exist_ok=True)
    series, dates = load_closes("99999999")
    dates = dates[-DAYS:]
    splits = {}
    if DECISIONS_PATH.exists():
        with DECISIONS_PATH.open(encoding="utf-8") as f:
            splits = {(r["symbol"], r["date"]): float(r["price_factor"]) for r in csv.DictReader(f) if r["decision"] != "move"}
    tickers = {p.stem.lstrip("_") for p in BY_TICKER.glob("*.json")}

    index = {d: i for i, d in enumerate(dates)}
    written = 0
    for ticker in sorted(tickers & set(series)):
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
    print(f"차트용 종가 {written}개 종목, {dates[0]}~{dates[-1]} → {out}")


if __name__ == "__main__":
    main()
