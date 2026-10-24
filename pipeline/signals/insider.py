"""내부자 장내 매수 신호. data/insider/purchases.csv(사용자 PC에서 매일 수집, pipeline/insider/form4.py)를 쓴다.

최근 90일 동안 임원·이사 여러 명이 장내 매수할수록(클러스터 매수) 점수가 높다.
- 같은 거래를 공동 보고자(펀드 그룹의 여러 법인 등)가 각자 공시하므로 같은 날·수량·가격은 1건으로 합친다
- 10% 대주주의 매수, 미리 정해 둔 계획 매수(10b5-1)는 낮게 반영한다
- 1인당 MIN_VALUE 미만은 이사회 보수의 주식 지급 등 제도성 매입일 가능성이 커서 매수자로 세지 않는다
매수가 없는 종목은 점수를 비워 둔다 (매수가 없는 게 보통이라 '매도 쪽' 근거가 아님).
"""

import csv
from datetime import date, timedelta
from pathlib import Path

WINDOW_DAYS = 90
TOP_TITLES = ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL", "PRESIDENT")
MIN_VALUE = 25_000  # 달러, 1인당 기간 합계


def insider_signals(path: Path, asof: str) -> dict[str, dict]:
    """ticker -> {ins_buyers, ins_value, ins_last, s_ins}. asof는 YYYYMMDD."""
    if not path.exists():
        return {}
    end = date(int(asof[:4]), int(asof[4:6]), int(asof[6:]))
    start = (end - timedelta(days=WINDOW_DAYS)).isoformat()
    trades: dict[tuple, dict] = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if start <= r["trade_date"] <= end.isoformat() and r["ticker"]:
                trades.setdefault((r["ticker"], r["trade_date"], r["shares"], r["price"]), r)

    by_ticker: dict[str, list] = {}
    for r in trades.values():
        by_ticker.setdefault(r["ticker"], []).append(r)

    out = {}
    for ticker, rows in by_ticker.items():
        per_owner: dict[str, float] = {}
        for r in rows:
            per_owner[r["owner"]] = per_owner.get(r["owner"], 0) + float(r["value"])
        rows = [r for r in rows if per_owner[r["owner"]] >= MIN_VALUE]
        if not rows:
            continue
        voluntary = [r for r in rows if r["is_10b5_1"] != "True"]
        insiders = {r["owner"] for r in voluntary if r["is_officer"] == "True" or r["is_director"] == "True"}
        holders = {r["owner"] for r in voluntary if r["owner"] not in insiders}
        planned = {r["owner"] for r in rows if r["is_10b5_1"] == "True"}
        top = any(t in r["title"].upper() for r in voluntary for t in TOP_TITLES)
        score = 35 * len(insiders) + 15 * len(holders) + 10 * len(planned) + (20 if top else 0)
        out[ticker] = {
            "ins_buyers": len(insiders),
            "ins_value": round(sum(float(r["value"]) for r in rows)),
            "ins_last": max(r["trade_date"] for r in rows),
            "s_ins": min(100.0, float(score)),
        }
    return out
