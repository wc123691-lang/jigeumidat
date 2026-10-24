"""SEC EDGAR Form 4에서 내부자 장내 매수(거래코드 P)만 모은다.

SEC가 GitHub Actions 접속을 막고 있어 사용자 PC에서 Windows 작업 스케줄러로 매일 실행한다.
하루 공시 약 500건, 다운로드 약 10MB. 마지막으로 처리한 날 다음부터 어제까지를 이어서 처리하므로
PC가 꺼져 있던 날은 다음 실행 때 몰아서 받는다.

    set SEC_USER_AGENT=Jigeumidat 연락처이메일
    python form4.py              # 밀린 날짜 처리 (처음이면 최근 30일)
    python form4.py --days 90    # 처음 시작할 때 거슬러 올라갈 일수

출력: data/insider/purchases.csv (매수 1건당 1행), data/insider/last_day.txt
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "insider"
PURCHASES = OUT_DIR / "purchases.csv"
LAST_DAY = OUT_DIR / "last_day.txt"

DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx"
ARCHIVES = "https://www.sec.gov/Archives/"

FIELDS = ["filed", "accession", "ticker", "issuer_cik", "issuer", "owner", "title",
          "is_director", "is_officer", "is_ten_percent", "trade_date", "shares", "price",
          "value", "shares_after", "is_10b5_1"]


def sec_get(url: str) -> bytes | None:
    """404(휴장일 색인 없음 등)이면 None."""
    req = urllib.request.Request(url, headers={"User-Agent": os.environ["SEC_USER_AGENT"]})
    time.sleep(0.12)  # SEC 요청 제한: 초당 10회
    try:
        return urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


_quarter_files: dict[str, set[str]] = {}


def index_exists(day: date) -> bool:
    """SEC는 없는 색인 파일(공휴일)에 404 대신 403을 주기도 해서, 분기 목록으로 실제 존재를 확인한다."""
    quarter = DAILY_INDEX.format(y=day.year, q=(day.month - 1) // 3 + 1, d="").rsplit("/", 1)[0]
    if quarter not in _quarter_files:
        listing = json.loads(sec_get(quarter + "/index.json"))
        _quarter_files[quarter] = {item["name"] for item in listing["directory"]["item"]}
    return f"form.{day.strftime('%Y%m%d')}.idx" in _quarter_files[quarter]


def form4_paths(day: date) -> list[str] | None:
    """그날 접수된 Form 4 (정정 포함) 공시 경로. 색인이 없는 날(휴일)은 None."""
    if not index_exists(day):
        return None
    raw = sec_get(DAILY_INDEX.format(y=day.year, q=(day.month - 1) // 3 + 1, d=day.strftime("%Y%m%d")))
    if raw is None:
        return None
    return [line.split()[-1] for line in raw.decode("latin-1").splitlines()
            if line.startswith(("4 ", "4/A "))]


def text(node, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def purchases(filing: str, filed: str, accession: str) -> list[dict]:
    start, end = filing.find("<ownershipDocument>"), filing.find("</ownershipDocument>")
    if start < 0 or end < 0:
        return []
    doc = ET.fromstring(filing[start : end + len("</ownershipDocument>")])
    owners = doc.findall("reportingOwner")
    owner = owners[0] if owners else None
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    base = {
        "filed": filed,
        "accession": accession,
        "ticker": text(doc, "issuer/issuerTradingSymbol").upper(),
        "issuer_cik": text(doc, "issuer/issuerCik"),
        "issuer": text(doc, "issuer/issuerName"),
        "owner": "; ".join(text(o, "reportingOwnerId/rptOwnerName") for o in owners),
        "title": text(rel, "officerTitle") if rel is not None else "",
        "is_director": text(rel, "isDirector") in ("1", "true") if rel is not None else False,
        "is_officer": text(rel, "isOfficer") in ("1", "true") if rel is not None else False,
        "is_ten_percent": text(rel, "isTenPercentOwner") in ("1", "true") if rel is not None else False,
        "is_10b5_1": text(doc, "aff10b5One") in ("1", "true"),
    }
    rows = []
    for t in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if text(t, "transactionCoding/transactionCode") != "P":
            continue
        shares = float(text(t, "transactionAmounts/transactionShares/value") or 0)
        price = float(text(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        rows.append({
            **base,
            "trade_date": text(t, "transactionDate/value"),
            "shares": shares,
            "price": price,
            "value": round(shares * price, 2),
            "shares_after": text(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
        })
    return rows


def process_day(day: date) -> list[dict] | None:
    paths = form4_paths(day)
    if paths is None:
        return None
    found = []
    for path in paths:
        raw = sec_get(ARCHIVES + path)
        if raw is None:
            continue
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        try:
            found += purchases(raw.decode("utf-8", "replace"), day.isoformat(), accession)
        except ET.ParseError:
            print(f"  XML 해석 실패: {accession}", file=sys.stderr)
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="처음 시작할 때 거슬러 올라갈 일수")
    args = parser.parse_args()
    if not os.environ.get("SEC_USER_AGENT"):
        sys.exit("SEC_USER_AGENT 환경변수를 '앱이름 연락처이메일' 형식으로 설정하세요 (SEC 요구사항).")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 하루치 색인은 미국 동부시간으로 그날이 끝나야 완성된다 (서머타임과 관계없이 UTC-5로 보수적으로 계산)
    today = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
    last = date.fromisoformat(LAST_DAY.read_text().strip()) if LAST_DAY.exists() else today - timedelta(days=args.days + 1)
    seen = set()
    if PURCHASES.exists():
        with PURCHASES.open(encoding="utf-8") as f:
            seen = {(r["accession"], r["trade_date"], r["shares"], r["price"]) for r in csv.DictReader(f)}

    day = last + timedelta(days=1)
    while day < today:  # 오늘 색인은 하루가 끝나야 완성되므로 어제까지만
        if day.weekday() < 5:
            rows = process_day(day)
            if rows is None:
                print(f"{day} 색인 없음 (휴일)")
            else:
                new = [r for r in rows if (r["accession"], r["trade_date"], str(r["shares"]), str(r["price"])) not in seen]
                write_header = not PURCHASES.exists()
                with PURCHASES.open("a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDS)
                    if write_header:
                        w.writeheader()
                    w.writerows(new)
                print(f"{day} 장내 매수 {len(new)}건")
        LAST_DAY.write_text(day.isoformat())
        day += timedelta(days=1)


if __name__ == "__main__":
    main()
