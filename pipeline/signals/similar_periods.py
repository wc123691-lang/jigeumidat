"""'지금과 비슷했던 시기' 과거 통계: 공포·탐욕 지수가 지금과 같은 구간에 들어갔던 때, 그 뒤 S&P500(SPY)은?

    python similar_periods.py     → data/signals/similar_periods.json

원칙
- 사건 단위로 센다: 구간에 '처음 들어간 날'만 사건. 구간 밖에서 20거래일 이상 지나야 새 사건으로 본다.
- 평상시(아무 날이나 샀을 때)와 비교한다.
- 평균만이 아니라 중앙값·승률·최악·보유 중 최대 하락폭을 함께 낸다. 사건이 10회 미만이면 참고용.
- SPY 가격 수익률이며 배당은 포함하지 않는다. IEX 종가 기준.
- 지수 백분위는 전체 이력 기준으로 매겨서, 과거 날짜의 지수에는 이후 데이터가 반영돼 있다 (설명용 통계).
"""

import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IEX_DIR = ROOT / "data" / "iex"
SIGNALS_DIR = ROOT / "data" / "signals"

BANDS = [(0, 25, "극단적 공포"), (25, 45, "공포"), (45, 55, "중립"), (55, 75, "탐욕"), (75, 101, "극단적 탐욕")]
HORIZONS = {"1개월": 21, "3개월": 63, "6개월": 126, "1년": 252}
DRAWDOWN_DAYS = 126
GAP_DAYS = 20
MIN_EVENTS = 10

# 잘 알려진 시장 사건 (YYYYMM 범위 → 이름). 범위 밖 사건은 날짜만 보여준다.
EPISODES = [
    ("201802", "201802", "2018년 2월 변동성 쇼크"),
    ("201810", "201812", "2018년 4분기 급락"),
    ("201905", "201906", "미중 무역분쟁 격화"),
    ("201908", "201908", "미중 무역전쟁·장단기 금리 역전"),
    ("201910", "201910", "2019년 10월 제조업 경기 우려"),
    ("202002", "202004", "코로나19 폭락"),
    ("202009", "202010", "2020년 가을 기술주 조정·미 대선 불안"),
    ("202111", "202112", "오미크론 변이·긴축 우려"),
    ("202201", "202212", "2022년 금리 인상 하락장"),
    ("202303", "202303", "SVB 은행 위기"),
    ("202310", "202310", "미 국채 금리 급등"),
    ("202408", "202408", "엔 캐리 트레이드 청산 급락"),
    ("202502", "202505", "2025년 관세 쇼크"),
]


def spy_closes() -> dict[str, float]:
    out = {}
    for path in sorted(IEX_DIR.glob("iex_????????.csv")):
        text = path.read_text(encoding="utf-8")
        start = text.find("\nSPY,")
        if start >= 0:
            out[path.stem[4:]] = float(text[start + 1 : text.find("\n", start + 1)].split(",")[1])
    return out


def band_of(value: float) -> tuple:
    return next(b for b in BANDS if b[0] <= value < b[1])


def episode(date: str) -> str | None:
    return next((name for lo, hi, name in EPISODES if lo <= date[:6] <= hi), None)


def summarize(returns: list[float]) -> dict:
    return {
        "n": len(returns),
        "mean": round(statistics.fmean(returns), 4),
        "median": round(statistics.median(returns), 4),
        "win_rate": round(sum(r > 0 for r in returns) / len(returns), 3),
        "worst": round(min(returns), 4),
        "best": round(max(returns), 4),
    }


def main() -> None:
    with (SIGNALS_DIR / "fear_greed_history.csv").open(encoding="utf-8") as f:
        fg = {r["date"]: float(r["fear_greed"]) for r in csv.DictReader(f)}
    spy = spy_closes()
    days = [d for d in sorted(spy) if d in fg]
    index = {d: i for i, d in enumerate(days)}
    current = fg[days[-1]]
    lo, hi, label = band_of(current)

    events, outside = [], GAP_DAYS
    for d in days:
        if lo <= fg[d] < hi:
            if outside >= GAP_DAYS:
                events.append(d)
            outside = 0
        else:
            outside += 1

    def forward(d: str, h: int) -> float | None:
        i = index[d] + h
        return spy[days[i]] / spy[d] - 1 if i < len(days) else None

    rows = []
    for d in events:
        window = [spy[x] for x in days[index[d] : index[d] + DRAWDOWN_DAYS + 1]]
        rows.append({
            "date": d,
            "fear_greed": fg[d],
            "episode": episode(d),
            "returns": {name: (round(r, 4) if (r := forward(d, h)) is not None else None) for name, h in HORIZONS.items()},
            "max_drawdown_6m": round(min(window) / spy[d] - 1, 4) if len(window) > DRAWDOWN_DAYS else None,
        })

    stats, baseline = {}, {}
    for name, h in HORIZONS.items():
        ev = [r["returns"][name] for r in rows if r["returns"][name] is not None]
        if ev:
            stats[name] = summarize(ev)
        base = [r for d in days if (r := forward(d, h)) is not None]
        baseline[name] = summarize(base)
    drawdowns = [r["max_drawdown_6m"] for r in rows if r["max_drawdown_6m"] is not None]

    result = {
        "as_of": days[-1],
        "period": [days[0], days[-1]],
        "current": {"fear_greed": current, "band": label, "band_range": [lo, min(hi, 100)]},
        "events": len(rows),
        "reference_only": len(rows) < MIN_EVENTS,
        "stats": stats,
        "baseline": baseline,
        "max_drawdown_6m": summarize(drawdowns) if drawdowns else None,
        "history": rows,
        "notes": "SPY 가격 수익률(배당 제외), IEX 종가 기준. 과거 결과는 미래 수익을 보장하지 않습니다.",
    }
    out = SIGNALS_DIR / "similar_periods.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"현재 {current} ({label}) · 사건 {len(rows)}회 → {out}")


if __name__ == "__main__":
    main()
