"""내부자 매수 과거 검증: '임원·이사 N명 이상이 30일 안에 장내에서 산 종목'은 그 뒤 실제로 올랐나.

- 사건: 한 종목에서 30일 안에 서로 다른 임원·이사(10% 대주주만인 법인·펀드 제외) N명 이상이 계획 매수가 아닌 장내 매수(P)를 한 첫날.
  같은 종목의 다음 사건은 90일이 지난 뒤부터 센다 (한 번의 매수 행렬을 여러 번 세지 않게).
- 들어간 날: N번째 매수가 공시된 다음 거래일 (공시를 보고 따라 산다고 가정). 종가 기준, 분할 보정(output/history).
- 3·6·12개월(63·126·252거래일) 뒤 수익률과 같은 기간 SPY 수익률을 비교한다.
- 한 사람이 산 금액 합계가 $25,000 미만인 매수는 뺀다 (앱의 내부자 신호와 같은 기준).

    python backtest.py

출력: data/insider/backtest.json
  {"asof", "horizons": [63, 126, 252], "rows": [[최소 인원, 사건 수, [기간마다: 평균, SPY 평균, 오른 비율, 중앙값, 사건 수, SPY보다 나은 비율] 또는 0], ...]}
한계: 가격 기록(output/history)은 최근 1년 안에 거래된 종목만 있어 상장폐지된 종목이 빠진다 → 실제보다 좋게 나온다.
"""

import bisect
import json
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BY_TICKER = ROOT / "data" / "insider" / "by_ticker"
HISTORY = ROOT / "output" / "history"
OUT = ROOT / "data" / "insider" / "backtest.json"
WINDOW = 30
COOLDOWN = 90
MIN_VALUE = 25_000
HORIZONS = (63, 126, 252)
PEOPLE = (1, 2, 3, 5)

DATES = json.loads((HISTORY / "_dates.json").read_text(encoding="utf-8"))


def closes(name: str) -> list | None:
    path = HISTORY / name
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    return [None] * d["start"] + d["c"]


def forward(c: list, day: str, h: int) -> float | None:
    i = bisect.bisect_right(DATES, day)  # 공시 다음 거래일
    if i + h >= len(c) or c[i] is None or c[i + h] is None:
        return None
    return c[i + h] / c[i] - 1


def events(rows: list, people: int) -> list[str]:
    """사건마다 들어갈 기준 공시일."""
    buys = sorted((r for r in rows if r[1] == "P" and r[9] == 0 and ("O" in r[3] or "D" in r[3])), key=lambda r: r[0])
    total = defaultdict(float)
    for r in buys:
        total[r[2]] += r[7]
    buys = [r for r in buys if total[r[2]] >= MIN_VALUE]
    out, last = [], None
    for i, r in enumerate(buys):
        start = (date.fromisoformat(r[0]) - timedelta(days=WINDOW)).isoformat()
        window = [b for b in buys[: i + 1] if b[0] >= start]
        if len({b[2] for b in window}) < people:
            continue
        if last and r[0] < (date.fromisoformat(last) + timedelta(days=COOLDOWN)).isoformat():
            continue
        last = r[0]
        out.append(max(b[10] for b in window))  # 마지막으로 공시된 날 (그때야 N명이 산 걸 안다)
    return out


def main() -> None:
    spy = closes("SPY.json")
    acc = {p: {h: ([], []) for h in HORIZONS} for p in PEOPLE}
    counts = defaultdict(int)
    for path in BY_TICKER.glob("*.json"):
        c = closes(path.name)
        if not c:
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        for p in PEOPLE:
            for filed in events(rows, p):
                counts[p] += 1
                for h in HORIZONS:
                    r, s = forward(c, filed, h), forward(spy, filed, h)
                    if r is not None and s is not None and -0.99 < r < 20:  # 단위 착오 같은 극단값은 뺀다
                        acc[p][h][0].append(r)
                        acc[p][h][1].append(s)
    out = []
    for p in PEOPLE:
        per = []
        for h in HORIZONS:
            rs, ss = acc[p][h]
            per.append([round(statistics.fmean(rs), 4), round(statistics.fmean(ss), 4), round(sum(r > 0 for r in rs) / len(rs), 4),
                        round(statistics.median(rs), 4), len(rs), round(sum(r > s for r, s in zip(rs, ss)) / len(rs), 4)] if len(rs) >= 30 else 0)
        out.append([p, counts[p], per])
        print(f"{p}명 이상: 사건 {counts[p]} · " + " · ".join(
            f"{h}일 평균 {x[0]:+.1%} (SPY {x[1]:+.1%}) 오른 비율 {x[2]:.0%} 중앙값 {x[3]:+.1%} SPY보다 나음 {x[5]:.0%}" for h, x in zip(HORIZONS, per) if x))
    OUT.write_text(json.dumps({"asof": DATES[-1], "horizons": list(HORIZONS), "rows": out}, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
