"""외부 순위(Unusual Whales 연간 보고서)와 우리 계산 대조용. 앱 데이터는 만들지 않는다.

Unusual Whales는 '연초 보유 종목 가치 → 연말 가치'로 의원 포트폴리오 수익률을 추정한다.
같은 방식으로 맞춰 보려고 2017년부터의 매매로 보유 종목을 쌓아 그해 수익률(수정 디츠 방식)을 낸다.
- 매수: 금액 구간 가운데 값만큼 그날 종가로 주식 수를 더한다.
- 전량 매도(S): 그 종목 보유를 모두 뺀다. 일부 매도: 금액 가운데 값만큼 뺀다(보유보다 많으면 전부).
- 보유 기간 중 가격이 없는 종목(상장폐지 등)은 뺀다.

    python validate.py 2024
"""

import csv
import sys
from collections import defaultdict

from house_returns import DATES, TRADES, closes, mid, price_on_or_after


def price(ticker: str, day: str) -> float | None:
    c = closes(ticker)
    p = price_on_or_after(c, day) if c else None
    return p[1] if p else None


def main() -> None:
    year = sys.argv[1] if len(sys.argv) > 1 else "2024"
    start, end = f"{year}-01-01", f"{year}-12-31"
    end_day = max(d for d in DATES if d <= end)
    start_day = min(d for d in DATES if d >= start)
    rows = [r for r in csv.DictReader(TRADES.open(encoding="utf-8"))
            if r["ticker"] and r["asset_type"] in ("ST", "OP") and r["trade_date"] <= end_day]
    rows.sort(key=lambda r: r["trade_date"])
    held: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))  # 의원 -> 티커 -> 주식 수
    flows: dict[str, list[tuple[str, float]]] = defaultdict(list)  # 그해 들어온 돈(+)/나간 돈(−)
    start_value: dict[str, float] = {}

    def value(m: str, day: str) -> float:
        return sum(sh * (price(t, day) or 0) for t, sh in held[m].items() if sh > 0)

    snap_done = False
    for r in rows + [None]:
        if not snap_done and (r is None or r["trade_date"] >= start_day):
            for m in list(held):
                start_value[m] = value(m, start_day)
            snap_done = True
        if r is None:
            break
        p = price(r["ticker"], r["trade_date"])
        if not p:
            continue
        m, amt = r["member"], mid(int(r["amount_lo"]), int(r["amount_hi"]))
        in_year = r["trade_date"] >= start_day
        if r["tx"] == "P":
            held[m][r["ticker"]] += amt / p
            if in_year:
                flows[m].append((r["trade_date"], amt))
        elif r["tx"].startswith("S"):
            sh = held[m][r["ticker"]] if r["tx"] == "S" else min(held[m][r["ticker"]], amt / p)
            held[m][r["ticker"]] -= sh
            if in_year:
                flows[m].append((r["trade_date"], -sh * p))

    spy = price("SPY", end_day) / price("SPY", start_day) - 1
    span = DATES.index(end_day) - DATES.index(start_day)
    result = []
    active = {r["member"] for r in rows if r["filed"][:4] == year}  # 그해 신고한 의원만 (떠난 의원의 옛 매수가 계속 보유로 남지 않게)
    for m in (set(start_value) | set(flows)) & active:
        v0, v1 = start_value.get(m, 0.0), value(m, end_day)
        # 수정 디츠: 그해 중간에 들어온 돈은 남은 기간만큼만 분모에 넣는다
        weighted = sum(f * (span - (DATES.index(min(d for d in DATES if d >= day)) - DATES.index(start_day))) / span for day, f in flows[m])
        net = sum(f for _, f in flows[m])
        base = v0 + weighted
        if base < 50_000:  # 너무 작은 포트폴리오는 몇 건에 크게 흔들린다
            continue
        result.append(((v1 - v0 - net) / base, m, base))
    result.sort(reverse=True)
    print(f"{year} SPY {spy:+.1%} · 의원 {len(result)}명")
    for i, (r, m, base) in enumerate(result[:25], 1):
        print(f"{i:2}. {m:30} {r:+.1%}  (평균 규모 ${base / 1e6:.1f}M)")
    for name in sys.argv[2:]:
        hit = [(i, r) for i, (r, m, _) in enumerate(result, 1) if name.lower() in m.lower()]
        print(name, hit[:1] or "없음")


if __name__ == "__main__":
    main()
