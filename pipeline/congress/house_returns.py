"""하원의원 매수 거래마다 수익률을 계산해 앱용 파일을 만든다.

매수(P) 한 건의 수익률 = 들어간 날 종가 → 나온 날 종가 (분할 보정, IEX 종가).
- 들어간 날: 거래일 기준(의원 본인의 실력)과 공시일 다음 거래일 기준(공시를 보고 따라 샀을 때) 두 가지.
- 나온 날: 같은 의원이 그 종목을 처음 판(S, S (partial)) 거래일. 아직 안 팔았으면 마지막 거래일.
  공시 기준은 판 사실도 공시된 다음 날 판 것으로 본다.
- 같은 기간 SPY 수익률을 함께 둬서 시장보다 얼마나 나았는지 비교한다.
- 옵션([OP])은 기초 주식 가격으로 대신한다 (옵션 손익은 공개 정보로 계산할 수 없다).
- 금액은 구간의 가운데 값으로 비중을 준다. 상한 없는 구간("Over $50,000,000")은 하한.

    python house_returns.py

출력: data/congress/house.json
  {"asof", "fields": [...], "members": [[이름, 지역구, 파일 이름], ...],
   "trades": [[의원 번호, 티커, 자산 유형, 소유자, 거래일, 공시일, 금액 하한, 금액 상한, 나온 날 또는 "",
               거래일 기준 수익률, 공시 기준 수익률, 같은 기간 SPY(거래일), SPY(공시)], ...]}
  수익률은 소수(0.12 = +12%), 가격이 없으면 null. 최근 거래가 앞.
      data/congress/members/<파일 이름>.json  의원 한 명의 전체 거래 (매도·교환 포함, 상세 화면용)
  파일 이름은 이름으로 만든 고정 값(nancy-pelosi)이라 실행마다 바뀌는 번호와 달리 앱 캐시와 어긋나지 않는다.
"""

import bisect
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRADES = ROOT / "output" / "congress" / "house_trades.csv"
HISTORY = ROOT / "output" / "history"
OUT = ROOT / "data" / "congress"

sys.path.insert(0, str(ROOT / "pipeline" / "signals"))
from export_prices import file_name  # noqa: E402  (CON 같은 윈도우 예약 이름 처리)

DATES = json.loads((HISTORY / "_dates.json").read_text(encoding="utf-8"))
_cache: dict[str, list] = {}


def closes(ticker: str) -> list | None:
    if ticker not in _cache:
        path = HISTORY / file_name(ticker)
        if not path.exists():
            _cache[ticker] = None
        else:
            d = json.loads(path.read_text(encoding="utf-8"))
            _cache[ticker] = [None] * d["start"] + d["c"]
    return _cache[ticker]


def price_on_or_after(c: list, day: str) -> tuple[int, float] | None:
    i = bisect.bisect_left(DATES, day)
    for j in range(i, min(i + 5, len(c))):  # 5거래일 안에 체결이 없으면 포기
        if c[j] is not None:
            return j, c[j]
    return None


def last_price(c: list) -> tuple[int, float] | None:
    for j in range(len(c) - 1, max(-1, len(c) - 10), -1):
        if c[j] is not None:
            return j, c[j]
    return None


def ret(c: list, start: str, end: str | None) -> float | None:
    a = price_on_or_after(c, start)
    b = price_on_or_after(c, end) if end else last_price(c)
    if not a or not b or b[0] < a[0]:
        return None
    return b[1] / a[1] - 1


def next_day(day: str) -> str:
    i = bisect.bisect_right(DATES, day)
    return DATES[i] if i < len(DATES) else "9999-12-31"


def mid(lo: int, hi: int) -> int:
    return lo if not hi else (lo + hi) // 2


TITLES = {"mr", "mrs", "ms", "dr", "honorable", "hon", "md", "facs", "phd", "esq"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def clean_name(name: str) -> str:
    """신고서마다 'Marjorie Taylor Mrs Greene', 'Mark Dr Green'처럼 호칭이 끼어 들어간 이름을 정리한다."""
    words = [t for t in name.replace(",", " ").split() if t.strip(".").lower() not in TITLES]
    return " ".join(t for i, t in enumerate(words) if i == 0 or t != words[i - 1])  # 'Scott Scott Franklin'


def person_key(name: str, district: str) -> tuple[str, str, str]:
    """같은 사람: 성 + 주 + 이름 첫 글자 ('Nicholas V. Taylor'와 'Nicholas Van Taylor'를 하나로)."""
    core = [t for t in clean_name(name).split() if t.strip(".").lower() not in SUFFIXES] or [name]
    return core[-1].lower(), district[:2], core[0][0].lower()


def main() -> None:
    rows = list(csv.DictReader(TRADES.open(encoding="utf-8")))
    # 같은 사람의 이름 표기를 가장 최근 신고서의 표기로 맞춘다
    latest: dict[tuple, tuple[str, str]] = {}
    for r in rows:
        k = person_key(r["member"], r["district"])
        if k not in latest or r["filed"] >= latest[k][0]:
            latest[k] = (r["filed"], clean_name(r["member"]))
    for r in rows:
        r["member"] = latest[person_key(r["member"], r["district"])][1]
    # 같은 신고서가 고쳐 다시 올라오면(수정 신고) 같은 거래가 두 번 나온다 → 의원·티커·종류·거래일·금액이 같으면 하나만
    seen, uniq = set(), []
    for r in rows:
        key = (r["member"], r["ticker"], r["owner"], r["tx"], r["trade_date"], r["amount_lo"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    rows = [r for r in uniq if r["ticker"] and r["asset_type"] in ("ST", "OP") and "1900" < r["trade_date"] <= r["filed"]]

    members: list[tuple[str, str, str]] = []
    member_idx: dict[str, int] = {}
    for r in rows:
        if r["member"] not in member_idx:
            member_idx[r["member"]] = len(members)
            members.append((r["member"], r["district"], re.sub(r"[^a-z0-9]+", "-", r["member"].lower()).strip("-")))

    sells: dict[tuple[str, str], list[tuple[str, str]]] = {}  # (의원, 티커) -> [(거래일, 공시일)]
    for r in rows:
        if r["tx"].startswith("S"):
            sells.setdefault((r["member"], r["ticker"]), []).append((r["trade_date"], r["filed"]))
    for v in sells.values():
        v.sort()

    spy = closes("SPY")
    out, by_member, missing = [], {}, 0
    for r in rows:
        mi = member_idx[r["member"]]
        by_member.setdefault(mi, []).append([r["ticker"], r["asset_type"], r["owner"], r["tx"], r["trade_date"], r["filed"],
                                             int(r["amount_lo"]), int(r["amount_hi"]), r["asset"]])
        if r["tx"] != "P":
            continue
        c = closes(r["ticker"])
        exit_ = next((s for s in sells.get((r["member"], r["ticker"]), []) if s[0] > r["trade_date"]), None)
        entry_d = next_day(r["filed"])
        exit_d = next_day(exit_[1]) if exit_ else None
        if exit_d and exit_d <= entry_d:
            exit_d = entry_d  # 매수와 매도가 한 신고서에 같이 공시된 경우: 따라 살 기회가 없었다
        rt = ret(c, r["trade_date"], exit_[0] if exit_ else None) if c else None
        rd = ret(c, entry_d, exit_d) if c else None
        if rt is None:
            missing += 1
        out.append([mi, r["ticker"], r["asset_type"], r["owner"], r["trade_date"], r["filed"], int(r["amount_lo"]), int(r["amount_hi"]),
                    exit_[0] if exit_ else "", rt, rd,
                    ret(spy, r["trade_date"], exit_[0] if exit_ else None), ret(spy, entry_d, exit_d)])

    rnd = lambda v: None if v is None else round(v, 4)
    out = [t[:9] + [rnd(v) for v in t[9:]] for t in out]
    out.sort(key=lambda t: (t[4], t[5]), reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "members").mkdir(exist_ok=True)
    (OUT / "house.json").write_text(json.dumps({
        "asof": DATES[-1], "members": members, "trades": out,
        "fields": ["member", "ticker", "asset_type", "owner", "trade_date", "filed", "amount_lo", "amount_hi", "exit_date",
                   "ret_trade", "ret_filed", "spy_trade", "spy_filed"],
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for mi, trades in by_member.items():
        trades.sort(key=lambda t: t[4], reverse=True)
        (OUT / "members" / f"{members[mi][2]}.json").write_text(json.dumps({"name": members[mi][0], "district": members[mi][1], "trades": trades},
                                                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"의원 {len(members)}명 · 매수 {len(out)}건 (가격 없음 {missing}) · 기준일 {DATES[-1]}")


if __name__ == "__main__":
    main()
