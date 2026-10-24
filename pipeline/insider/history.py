"""종목별 내부자 거래 내역 (최근 5년, 장내 매수 P·장내 매도 S) → 앱의 '내부자' 화면용 파일.

과거분은 SEC가 분기마다 공개하는 Form 3·4·5 정리 데이터셋(Insider Transactions Data Sets)에서,
아직 데이터셋이 나오지 않은 최근분은 form4.py가 매일 모은 purchases.csv·sales.csv에서 가져온다.
SEC가 GitHub Actions 접속을 막아 사용자 PC에서 form4.py 다음에 실행한다 (run_daily.bat).

- 분기 데이터셋은 한 번 받아 정리한 결과를 output/form345/<분기>.csv에 두고 다시 받지 않는다 (압축 파일은 지운다).
- 회사(CIK) 단위로 묶어 티커가 바뀐 회사(FB → META)도 이어지게 하고, 지금 우리가 다루는 종목(scores_latest.json)만 내보낸다.
- 같은 공시·같은 날·같은 거래코드의 여러 줄(가격대별 분할 체결)은 한 줄로 합친다 (평균 가격).
- 공시 가격이 그날 IEX 종가와 3배 넘게 다르면 잘못 적힌 공시(가격 칸에 총액, 단위 착오 등)로 보고 뺀다.
  안 빼면 '1주당 $2,272,653' 같은 공시 하나가 매수액 순위 맨 위를 차지한다.

    python history.py

출력: data/insider/by_ticker/<티커>.json (CON 같은 윈도우 예약 이름은 _CON.json)
  {"ticker", "issuer", "rows": [[거래일, 코드, 이름, 관계, 직책, 주식수, 평균가격, 금액, 거래 후 보유, 10b5-1 여부, 공시일], ...]}
  관계: D 이사, O 임원, T 10% 이상 주주 (여러 개면 붙여 씀). 최근 거래가 앞.
      data/insider/summary.json  종목별·사람 유형별·기간별 합계 (내부자 탭 첫 화면, 거래일 기준)
  {"asof", "periods": [30, 90, 180, 365], "groups": ["all", "insider", "officer"],
   "rows": [[티커, 회사, [유형마다: [마지막 매수일, 마지막 매도일, [기간마다: 매수액, 매수자 수, 매수 건수,
             매도액, 매도자 수, 매도 건수, 계획 매도액, 최대 보유 증가율] 또는 0] 또는 0]], ...]}
  유형: all 전체, insider 임원·이사(10% 대주주만인 법인·펀드 제외), officer 임원만.
  보유 증가율: 매수 한 건의 주식 수 / 거래 전 보유(거래 후 보유 − 매수 주식 수), 최대 MAX_GROWTH.
  거래 전 보유가 0인 신규 매수(상장 때 처음 배정받는 경우 등)는 증가율 비교에서 뺀다 (모두 '무한대'가 되어 순위를 덮는다).
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSIDER = ROOT / "data" / "insider"
OUT = INSIDER / "by_ticker"
SUMMARY = INSIDER / "summary.json"
PERIODS = (30, 90, 180, 365)  # 앱의 1개월·3개월·6개월·1년
GROUPS = {"all": lambda role: True, "insider": lambda role: "O" in role or "D" in role, "officer": lambda role: "O" in role}
MAX_GROWTH = 10.0  # 보유 증가율 상한 (10배)
CACHE = ROOT / "output" / "form345"
UNIVERSE = ROOT / "data" / "signals" / "scores_latest.json"
IEX = ROOT / "data" / "iex"
PRICE_TOLERANCE = 3  # 공시 가격 / 종가가 1/3 ~ 3배 밖이면 뺀다
LISTING = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
FIRST_QUARTER = "2021q4"  # 최근 5년
CODES = ("P", "S")
COLUMNS = ["issuer_cik", "symbol", "issuer", "accession", "filed", "trade_date", "code", "owner", "role", "title",
           "shares", "value", "shares_after", "plan"]

csv.field_size_limit(10**8)


def sec_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": os.environ["SEC_USER_AGENT"]})
    time.sleep(0.12)  # SEC 요청 제한: 초당 10회
    return urllib.request.urlopen(req, timeout=300).read()


def sec_date(s: str) -> str:
    return datetime.strptime(s, "%d-%b-%Y").date().isoformat() if s else ""


def role_of(relationship: str) -> str:
    return "".join(letter for word, letter in (("Director", "D"), ("Officer", "O"), ("TenPercentOwner", "T")) if word in relationship)


def quarter_rows(raw_zip: bytes) -> list[dict]:
    """분기 데이터셋 하나 → 장내 매수·매도 (공시·날짜·코드별로 합친 행)."""
    z = zipfile.ZipFile(io.BytesIO(raw_zip))
    read = lambda name: csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8"), delimiter="\t")
    subs = {r["ACCESSION_NUMBER"]: r for r in read("SUBMISSION.tsv")}
    owners: dict[str, list[dict]] = defaultdict(list)
    for r in read("REPORTINGOWNER.tsv"):
        owners[r["ACCESSION_NUMBER"]].append(r)
    plan_notes = {(r["ACCESSION_NUMBER"], r["FOOTNOTE_ID"]) for r in read("FOOTNOTES.tsv") if "10b5-1" in r["FOOTNOTE_TXT"]}

    merged: dict[tuple, dict] = {}
    for t in read("NONDERIV_TRANS.tsv"):
        code = t["TRANS_CODE"]
        sub = subs.get(t["ACCESSION_NUMBER"])
        if code not in CODES or sub is None:
            continue
        acc = t["ACCESSION_NUMBER"]
        shares = float(t["TRANS_SHARES"] or 0)
        price = float(t["TRANS_PRICEPERSHARE"] or 0)
        # 이 거래에 달린 각주 중 10b5-1(미리 정해 둔 매매 계획)을 말하는 것이 있으면 계획 거래
        notes = {fid.strip() for k, v in t.items() if k.endswith("_FN") and v for fid in v.split(",")}
        # 10b5-1 체크 칸(AFF10B5ONE)은 2023년 이전 데이터셋엔 없다
        plan = sub.get("AFF10B5ONE") in ("1", "true") or any((acc, fid) in plan_notes for fid in notes)
        key = (acc, sec_date(t["TRANS_DATE"]), code)
        row = merged.get(key)
        if row is None:
            people = owners.get(acc, [])
            row = merged[key] = {
                "issuer_cik": sub["ISSUERCIK"].lstrip("0"), "symbol": sub["ISSUERTRADINGSYMBOL"], "issuer": sub["ISSUERNAME"],
                "accession": acc, "filed": sec_date(sub["FILING_DATE"]), "trade_date": key[1], "code": code,
                "owner": "; ".join(p["RPTOWNERNAME"] for p in people),
                "role": "".join(sorted({c for p in people for c in role_of(p["RPTOWNER_RELATIONSHIP"])})),
                "title": next((p["RPTOWNER_TITLE"] for p in people if p["RPTOWNER_TITLE"]), ""),
                "shares": 0.0, "value": 0.0, "shares_after": "", "plan": False,
            }
        row["shares"] += shares
        row["value"] += shares * price
        row["shares_after"] = t["SHRS_OWND_FOLWNG_TRANS"] or row["shares_after"]
        row["plan"] = row["plan"] or plan
    return list(merged.values())


def cached_quarters() -> list[Path]:
    """새로 나온 분기 데이터셋을 받아 정리해 둔다. 이미 정리한 분기는 건너뛴다."""
    CACHE.mkdir(parents=True, exist_ok=True)
    page = sec_get(LISTING).decode("utf-8", "replace")
    links = dict((q, path) for path, q in re.findall(r'href="(/files/[^"]*?/(\d{4}q\d)_form345\.zip)"', page))
    for q in sorted(links):
        target = CACHE / f"{q}.csv"
        if q < FIRST_QUARTER or target.exists():
            continue
        print(f"{q} 데이터셋 받는 중", flush=True)
        rows = quarter_rows(sec_get("https://www.sec.gov" + links[q]))
        with target.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)
        print(f"{q} 장내 매수·매도 {len(rows)}건", flush=True)
    return sorted(CACHE.glob("*.csv"))


def daily_rows() -> list[dict]:
    """form4.py가 매일 모은 최근분 (분기 데이터셋과 겹치는 공시는 나중에 걸러진다)."""
    merged: dict[tuple, dict] = {}
    for code, name in (("P", "purchases.csv"), ("S", "sales.csv")):
        path = INSIDER / name
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["trade_date"] = r["trade_date"][:10]  # Form 4 XML 날짜에 시간대가 붙기도 한다 (2026-09-28-05:00)
                key = (r["accession"], r["trade_date"], code)
                row = merged.get(key)
                if row is None:
                    role = "".join(c for c, flag in (("D", "is_director"), ("O", "is_officer"), ("T", "is_ten_percent")) if r[flag] == "True")
                    row = merged[key] = {
                        "issuer_cik": r["issuer_cik"].lstrip("0"), "symbol": r["ticker"], "issuer": r["issuer"],
                        "accession": r["accession"], "filed": r["filed"], "trade_date": r["trade_date"], "code": code,
                        "owner": r["owner"], "role": role, "title": r["title"],
                        "shares": 0.0, "value": 0.0, "shares_after": "", "plan": r["is_10b5_1"] == "True",
                    }
                row["shares"] += float(r["shares"] or 0)
                row["value"] += float(r["value"] or 0)
                row["shares_after"] = r["shares_after"] or row["shares_after"]
    return list(merged.values())


def candidate_tickers(symbol: str, universe: set[str]) -> set[str]:
    """공시의 티커 칸('BRK.A', 'BFA, BFB' 등) → 우리 종목 목록의 티커들. 클래스 주식은 같은 회사의 다른 클래스도 함께.

    티커 칸은 사람이 적어서 'N/A', 'NYSE: KRC', 'Z AND ZG', 'N O G' 같은 값이 섞여 있다.
    'N/A'를 나눠 'A'(애질런트)로 잇는 일이 없게, 여러 조각일 때 한 글자 조각은 쓰지 않는다.
    """
    s = re.sub(r"\b(NYSE|NASDAQ|AMEX|NYSEAMERICAN|OTC|OTCQB|OTCQX|OTCBB)\b\s*[:/]?", " ", symbol.upper())
    if s.strip() in ("N/A", "NA", "NONE", "-"):
        return set()
    tokens = [t for t in re.split(r"[\s,;/]+", s) if t and t != "AND"]
    if len(tokens) > 1 and all(len(t) == 1 for t in tokens):
        tokens = ["".join(tokens)]  # N O G → NOG
    found = set()
    for token in tokens:
        if len(token) == 1 and len(tokens) > 1:
            continue
        options = {token, token.replace("-", ".")}
        if len(token) >= 3 and "." not in token:
            options.add(token[:-1] + "." + token[-1])  # BFB → BF.B
        found |= options & universe
        for t in options:
            # 클래스 주식(BRK.A)은 우리 목록에 없는 클래스여도 같은 회사의 다른 클래스(BRK.B)로 잇는다
            if "." in t and (t in universe or token == t):
                root = t.split(".")[0] + "."
                found |= {u for u in universe if u.startswith(root)}
    return found


# 윈도우에서 파일 이름으로 쓸 수 없는 이름(CON 등)은 앞에 _를 붙인다. 앱(src/domain/insider.ts)도 같은 규칙.
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM\d|LPT\d)$")


def file_name(ticker: str) -> str:
    return ("_" if RESERVED.match(ticker) else "") + ticker + ".json"


_closes: dict[str, dict[str, float]] = {}


def close_near(day: str, ticker: str) -> float | None:
    """거래일(휴장이면 5일 전까지)의 IEX 종가."""
    d = date.fromisoformat(day)
    for back in range(6):
        key = (d - timedelta(days=back)).strftime("%Y%m%d")
        if key not in _closes:
            path = IEX / f"iex_{key}.csv"
            _closes[key] = {}
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    _closes[key] = {r["symbol"]: float(r["iex_last_price"]) for r in csv.DictReader(f) if r["iex_last_price"]}
        if ticker in _closes[key]:
            return _closes[key][ticker]
    return None


def plausible(r: dict, ticker: str) -> bool:
    shares, value = float(r["shares"]), float(r["value"])
    if value <= 0:
        return True  # 가격 없는 거래(증여성 등)는 금액에 영향이 없다
    close = close_near(r["trade_date"], ticker)
    return close is None or 1 / PRICE_TOLERANCE <= (value / shares) / close <= PRICE_TOLERANCE


def holding_growth(r: dict) -> float | None:
    """매수로 보유 주식이 몇 배 늘었나 (0.25 = 25% 증가). 거래 후 보유를 모르거나, 앞뒤가 안 맞거나, 신규 매수면 None."""
    if not r["shares_after"]:
        return None
    shares, after = float(r["shares"]), float(r["shares_after"])
    before = after - shares
    if before < -0.5:
        return None  # 직접·간접 보유 줄이 섞여 거래 후 보유가 매수량보다 작은 경우
    return None if before < 1 else min(MAX_GROWTH, shares / before)


def main() -> None:
    if not os.environ.get("SEC_USER_AGENT"):
        sys.exit("SEC_USER_AGENT 환경변수를 '앱이름 연락처이메일' 형식으로 설정하세요 (SEC 요구사항).")
    universe = set(json.loads(UNIVERSE.read_text(encoding="utf-8"))["data"])
    first_day = f"{FIRST_QUARTER[:4]}-{(int(FIRST_QUARTER[-1]) - 1) * 3 + 1:02d}-01"

    rows: dict[tuple, dict] = {}
    for path in cached_quarters():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["accession"], r["trade_date"], r["code"])] = r
    for r in daily_rows():
        rows.setdefault((r["accession"], r["trade_date"], r["code"]), r)

    # 회사별로 묶고, 티커마다 그 티커를 가장 최근에 쓴 회사를 고른다 (티커 재사용 대비)
    by_cik: dict[str, list[dict]] = defaultdict(list)
    claims: dict[str, tuple[str, str]] = {}  # 티커 → (최근 공시일, CIK)
    for r in rows.values():
        if not r["trade_date"] or r["trade_date"] < first_day or float(r["shares"]) <= 0:
            continue
        by_cik[r["issuer_cik"]].append(r)
        for t in candidate_tickers(r["symbol"], universe):
            if t not in claims or r["filed"] > claims[t][0]:
                claims[t] = (r["filed"], r["issuer_cik"])

    # 회사마다 대표 티커 하나 (요약 목록과 가격 확인용)
    ticker_of: dict[str, str] = {}
    for ticker, (_, cik) in sorted(claims.items()):
        ticker_of.setdefault(cik, ticker)
    dropped = 0
    for cik, ticker in ticker_of.items():
        kept = [r for r in by_cik[cik] if plausible(r, ticker)]
        dropped += len(by_cik[cik]) - len(kept)
        by_cik[cik] = kept

    OUT.mkdir(parents=True, exist_ok=True)
    written = set()
    today = date.today().isoformat()
    for ticker, (_, cik) in sorted(claims.items()):
        trades = sorted(by_cik[cik], key=lambda r: (r["trade_date"], r["filed"]), reverse=True)
        if not trades:
            continue
        out = []
        for r in trades:
            shares, value = float(r["shares"]), float(r["value"])
            after = float(r["shares_after"]) if r["shares_after"] else None
            out.append([r["trade_date"], r["code"], r["owner"], r["role"], r["title"], round(shares, 4),
                        round(value / shares, 4), round(value), after, 1 if r["plan"] in (True, "True") else 0, r["filed"]])
        body = {"ticker": ticker, "issuer": trades[0]["issuer"], "rows": out}
        path = OUT / file_name(ticker)
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        # 내용이 같으면 다시 쓰지 않아 매일 커밋이 바뀐 종목만 담게 한다
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        written.add(path.name)
    for stale in OUT.glob("*.json"):
        if stale.name not in written:
            stale.unlink()

    # 내부자 탭 첫 화면: 종목별로 사람 유형·기간마다 매수·매도 합계. 한 회사의 여러 클래스(BF.A·BF.B)는 대표 티커 하나로만 센다.
    since = [(date.today() - timedelta(days=d)).isoformat() for d in PERIODS]
    value = lambda rs: round(sum(float(r["value"]) for r in rs))
    summary = []
    for cik, ticker in sorted(ticker_of.items(), key=lambda x: x[1]):
        recent = [r for r in by_cik[cik] if r["trade_date"] >= since[-1]]
        if not recent:
            continue
        groups = []
        for keep in GROUPS.values():
            mine = [r for r in recent if keep(r["role"])]
            if not mine:
                groups.append(0)
                continue
            stats = []
            for start in since:
                s = [r for r in mine if r["trade_date"] >= start]
                buys = [r for r in s if r["code"] == "P"]
                sells = [r for r in s if r["code"] == "S"]
                growth = max((g for g in map(holding_growth, buys) if g is not None), default=0)
                stats.append([value(buys), len({r["owner"] for r in buys}), len(buys),
                              value(sells), len({r["owner"] for r in sells}), len(sells),
                              value([r for r in sells if r["plan"] in (True, "True")]), round(growth, 4)] if s else 0)
            last = lambda code: max((r["trade_date"] for r in mine if r["code"] == code), default="")
            groups.append([last("P"), last("S"), stats])
        summary.append([ticker, max(recent, key=lambda r: r["filed"])["issuer"], groups])
    SUMMARY.write_text(json.dumps({"asof": today, "periods": list(PERIODS), "groups": list(GROUPS), "rows": summary},
                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    old_feed = INSIDER / "latest.json"  # 예전 첫 화면 파일 (최근 30일 공시 목록)
    if old_feed.exists():
        old_feed.unlink()
    print(f"{today} 종목 {len(written)}개 내부자 거래 내역 저장 (가격이 종가와 크게 달라 뺀 공시 {dropped}건)")


if __name__ == "__main__":
    main()
