"""전체 기간(2016-12~) 종가와 액면분할 판정. 내 신호 시뮬레이션용 긴 가격 파일(export_history.py)이 쓴다.

compute_signals.py는 최근 300일만 다루므로 그보다 오래된 분할은 판정해 두지 않았다.
이 파일을 직접 실행하면 전체 기간의 분할 후보를 SEC 발행주식수로 확인해 split_decisions_full.csv에 쌓는다.
SEC가 GitHub Actions 접속을 막으므로 사용자 PC에서 실행한다 (이미 판정한 후보는 다시 묻지 않는다).

    python price_history.py
"""

import array
import csv
import math
import sys
from pathlib import Path

from compute_signals import (DECISION_FIELDS as FIELDS, DECISIONS_PATH, FULL_DECISIONS, IEX_DIR, SEC_TICKERS, SecUnavailable,
                             applies, detect_split, judge_split, sec_get)
NAN = float("nan")


def load_aligned(days: int | None = None) -> tuple[list[str], dict[str, array.array]]:
    """(거래일 YYYYMMDD 목록, 종목 -> 그 거래일 순서의 종가 배열(체결 없던 날은 NaN))."""
    files = sorted(IEX_DIR.glob("iex_????????.csv"))
    if days:
        files = files[-days:]
    n = len(files)
    series: dict[str, array.array] = {}
    for i, path in enumerate(files):
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not r["iex_last_price"]:
                    continue
                arr = series.get(r["symbol"])
                if arr is None:
                    arr = series[r["symbol"]] = array.array("d", [NAN]) * n
                arr[i] = float(r["iex_last_price"])
    return [p.stem[4:] for p in files], series


def candidates(dates: list[str], series: dict[str, array.array]) -> list[tuple[str, str, float, bool]]:
    """(종목, 날짜, 배율, 비율이 딱 맞는지)."""
    found = []
    for symbol, arr in series.items():
        prev = None
        for i, v in enumerate(arr):
            if math.isnan(v):
                continue
            if prev is not None:
                hit = detect_split(prev, v)
                if hit:
                    found.append((symbol, dates[i], *hit))
            prev = v
    return found


def load_decisions() -> dict[tuple[str, str], dict]:
    """전체 기간 판정 + 최근 판정(compute_signals.py). 같은 후보면 최근 판정을 따른다."""
    out = {}
    for path in (FULL_DECISIONS, DECISIONS_PATH):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out.update({(r["symbol"], r["date"]): r for r in csv.DictReader(f)})
    return out


def split_factors(dates: list[str], series: dict[str, array.array]) -> dict[tuple[str, str], float]:
    """보정할 (종목, 날짜) -> 가격 배율. 판정이 있으면 그대로(compute_signals.applies), 없으면 비율이 딱 맞을 때만."""
    decisions = load_decisions()
    out = {}
    for s, d, f, exact in candidates(dates, series):
        row = decisions.get((s, d))
        if (applies(row) if row else exact):
            out[(s, d)] = float(row["price_factor"]) if row else f  # SEC로 확인한 배율이 있으면 그것 (TSLA 2020 5:1)
    return out


def adjusted(symbol: str, arr: array.array, dates: list[str], splits: dict) -> list[float]:
    """분할 보정한 종가 (NaN은 그대로). 분할일 이전 가격에 배율을 곱한다."""
    out = list(arr)
    factor = 1.0
    for i in range(len(out) - 1, -1, -1):
        if not math.isnan(out[i]):
            out[i] *= factor
        f = splits.get((symbol, dates[i]))
        if f:
            factor *= f
    return out


def save(rows: dict) -> None:
    with FULL_DECISIONS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="True")  # 예전 줄(exact 칸 없음)은 딱 맞는 후보였다
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["date"], r["symbol"])))


def main() -> None:
    dates, series = load_aligned()
    found = sorted(candidates(dates, series), key=lambda c: (c[1], c[0]))
    saved = {}
    if FULL_DECISIONS.exists():
        with FULL_DECISIONS.open(encoding="utf-8") as f:
            saved = {(r["symbol"], r["date"]): r for r in csv.DictReader(f)}
    todo = [c for c in found if saved.get(c[:2], {}).get("decision") in (None, "pending")]
    print(f"{dates[0]}~{dates[-1]} 분할 후보 {len(found)}개, 새로 판정할 것 {len(todo)}개", flush=True)
    ciks = {v["ticker"]: v["cik_str"] for v in sec_get(SEC_TICKERS).values()}
    rows = dict(saved)
    for n, (symbol, date, factor, exact) in enumerate(todo, 1):
        try:
            decision, reason, *confirmed = judge_split(ciks.get(symbol.replace(".", "-")), date, factor, exact)
            factor = confirmed[0] if confirmed else factor
        except SecUnavailable as e:
            decision, reason = "pending", f"SEC 조회 실패({e})"
        except Exception as e:  # 공시 데이터가 이상한 회사 하나 때문에 전체가 멈추지 않게
            decision, reason = "pending", f"판정 오류({type(e).__name__}: {e})"
        rows[(symbol, date)] = {"symbol": symbol, "date": date, "price_factor": factor, "exact": exact, "decision": decision, "reason": reason}
        if n % 100 == 0:
            save(rows)  # 중간에 멈춰도 다음 실행이 이어서 하게
            print(f"  {n}/{len(todo)}", flush=True)
    save(rows)
    counts = {}
    for r in rows.values():
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
    print(f"판정 {counts} → {FULL_DECISIONS}")


if __name__ == "__main__":
    sys.exit(main())
