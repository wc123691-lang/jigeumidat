"""내 신호 시뮬레이션용 전체 기간(2016-12~) 종가 (분할 보정, IEX 종가 기준).

전 종목 파일이 커서(수십~백수십 MB) 매일이 아니라 한 달에 한 번 만들고, 기록 없이 'history' 브랜치에 덮어쓴다
(.github/workflows/history_monthly.yml). 최근 1년은 매일 갱신되는 'prices' 브랜치(export_prices.py)를 앱이 이어 붙인다.
그 사이 새로 생긴 분할은 앱이 두 파일이 겹치는 날의 가격 비율로 맞춘다.

분할 판정은 price_history.py가 PC에서 SEC로 확인해 둔 split_decisions_full.csv(+ 최근 판정)를 쓴다.

    python export_history.py [출력 폴더]   # 기본 output/history

출력: _dates.json  ["YYYY-MM-DD", ...]   (전체 거래일)
      <티커>.json   {"start": 첫 거래일 위치, "c": [종가 또는 null, ...]}  (start부터 끝까지)
      CON 같은 윈도우 예약 이름은 _CON.json
"""

import json
import math
import sys
from pathlib import Path

from export_prices import file_name
from price_history import adjusted, load_aligned, split_factors

ROOT = Path(__file__).resolve().parents[2]
RECENT_DAYS = 252  # 최근 1년 안에 체결이 있는 종목만 (상장폐지 종목은 고를 일이 없다)
MIN_POINTS = 260  # 신호 계산(200일선)에 필요한 최소 거래일 + 여유


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "history"
    out.mkdir(parents=True, exist_ok=True)
    dates, series = load_aligned()
    splits = split_factors(dates, series)
    written = 0
    for symbol, arr in sorted(series.items()):
        if all(math.isnan(v) for v in arr[-RECENT_DAYS:]) or sum(not math.isnan(v) for v in arr) < MIN_POINTS:
            continue
        prices = adjusted(symbol, arr, dates, splits)
        start = next(i for i, v in enumerate(prices) if not math.isnan(v))
        closes = [None if math.isnan(v) else float(f"{v:.5g}") for v in prices[start:]]
        (out / file_name(symbol)).write_text(json.dumps({"start": start, "c": closes}, separators=(",", ":")), encoding="utf-8")
        written += 1
    (out / "_dates.json").write_text(json.dumps([f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]), encoding="utf-8")
    print(f"전체 기간 종가 {written}개 종목, {dates[0]}~{dates[-1]}, 분할 보정 {len(splits)}건 → {out}")


if __name__ == "__main__":
    main()
