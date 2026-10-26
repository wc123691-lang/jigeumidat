"""하원의원 주식 거래 신고(PTR, Periodic Transaction Report) 수집.

STOCK Act에 따라 하원의원(배우자·부양 자녀 포함)은 $1,000 넘는 거래를 거래 후 최대 45일 안에 신고한다.
하원 서기국(House Clerk)이 연도별 신고 목록(<연도>FD.zip)과 신고서 PDF를 공개한다.

- 목록에서 FilingType P(PTR)만 골라 신고서 PDF를 받는다. 받은 PDF는 output/congress/ptr/<연도>/<DocID>.pdf에 두고 다시 받지 않는다.
- 전자 신고서는 글자를 읽을 수 있어 거래 줄을 뽑는다. 손으로 써서 스캔한 신고서(글자 없음)는 건너뛰고 개수만 센다.
- 금액은 구간으로만 신고된다 ($1,001 - $15,000 등).
- 읽은 결과는 output/congress/parsed/<연도>.json에 두어 매일 실행 때 새 신고서만 읽는다.

    python house.py [시작 연도]   (기본 2017)

출력: output/congress/house_trades.csv
  doc_id, member, district, filed, owner, ticker, asset, asset_type, tx, trade_date, notified, amount_lo, amount_hi
  owner: '' 본인, SP 배우자, JT 공동, DC 부양 자녀. tx: P 매수, S 매도, S (partial) 일부 매도, E 교환.
  asset_type: [ST] 주식, [OP] 옵션, [GS] 국채 등 (https://fd.house.gov/reference/asset-type-codes.aspx)
"""

import csv
import json
import io
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "congress"
INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
UA = {"User-Agent": "Mozilla/5.0 (jigeumidat data pipeline)"}

FIELDS = ["doc_id", "member", "district", "filed", "owner", "ticker", "asset", "asset_type", "tx",
          "trade_date", "notified", "amount_lo", "amount_hi"]

# 거래 한 줄: [소유자] 자산 이름 [유형] 거래종류 거래일 통지일 금액. 2020년 무렵까지의 옛 양식에는 [유형]이 없다.
TX = re.compile(
    r"(?P<asset>.*?)(?:\[(?P<type>[A-Za-z]{2})\])?\s*"
    r"(?P<tx>P|S \(partial\)|S|E)\s+(?P<date>\d{2}/\d{2}/\d{4})\s*(?P<notified>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<amount>Over \$[\d,]+|\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\s*-|Spouse/DC Over \$[\d,]+)", re.S)
# 옛 양식은 글꼴 탓에 대소문자가 섞여 나온다 "(MsFT)" → 대문자로 바꿔 쓴다
TICKER = re.compile(r"\(([A-Za-z][A-Za-z0-9.\-]{0,6})\)")
AMOUNT = re.compile(r"\$([\d,]+)")
OLD_JUNK = re.compile(r"laSS|etailS|nitial|mlkj|fedcb|ertification|Filing ID")
# 거래 줄 사이에 끼는 부가 정보 줄(필링 상태 "F S : New", 하위 계좌 "S O : ...", 설명 "D : ...", 표 머리)
# PDF 글꼴 탓에 소문자가 빠져 "F      S     : New"처럼 대문자와 빈칸만 남는다
NOISE_LINE = re.compile(r"^([A-Z](\s+[A-Z])*\s*:|Description:|Type$|Date$|Date Notification|Amount Cap|Gains >|\$200\?|ID Owner|\* For the complete)")


def get(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(url)


def ptr_index(year: int) -> list[dict]:
    raw = get(INDEX_URL.format(year=year))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ET.fromstring(z.read(f"{year}FD.xml").decode("utf-8-sig"))
    out = []
    for m in root.iter("Member"):
        if (m.findtext("FilingType") or "") != "P":
            continue
        name = " ".join(x for x in (m.findtext("First"), m.findtext("Last"), m.findtext("Suffix")) if x)
        filed = datetime.strptime(m.findtext("FilingDate"), "%m/%d/%Y").date().isoformat()
        out.append({"doc_id": m.findtext("DocID"), "member": name, "district": m.findtext("StateDst") or "", "filed": filed})
    return out


def pdf_text(path: Path) -> str:
    try:
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    except Exception:
        return ""


def parse(text: str) -> list[dict]:
    # 표 머리 뒤부터 읽는다 (머리 앞에는 이름·지역구만 있다)
    head = text.find("$200?")
    body = text[head + 5:] if head >= 0 else text
    rows = []
    for m in TX.finditer(body.replace("\x00", " ")):
        # 자산 이름: [유형] 바로 앞에서 거꾸로 올라가며 부가 정보 줄을 만나면 멈춘다
        lines = []
        for line in reversed(m.group("asset").rstrip().split("\n")):
            line = line.strip()
            # 옛 양식은 부가 정보 줄의 글자가 깨져("IlINg S TATuS : New", "SSet c laSS D etailS") 콜론·짧은 줄·깨진 머리말로 끊는다
            if not line or NOISE_LINE.match(line) or ":" in line or len(line) <= 2 or OLD_JUNK.search(line):
                break
            lines.insert(0, line)
        asset = " ".join(" ".join(lines).split())
        # 소유자 표시(SP 배우자 등)로 시작하는 줄이 있으면 그 줄부터가 자산 이름이다
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].split(" ", 1)[0] in ("SP", "JT", "DC"):
                asset = " ".join(" ".join(lines[i:]).split())
                break
        owner = ""
        first = asset.split(" ", 1)
        if first[0] in ("SP", "JT", "DC"):
            owner, asset = first[0], (first[1] if len(first) > 1 else "")
        # 티커: [유형] 바로 앞 150자 안의 마지막 괄호. 옛 양식은 자산 이름이 앞 거래의 설명 줄에 붙어 나와 줄 단위로는 놓친다
        tail = m.group("asset")[-150:].rstrip()
        tickers = [t.upper() for t in TICKER.findall(tail)]
        if not asset and tickers:
            asset = " ".join(tail.split("\n")[-1].split(":")[-1].split())
        nums = [int(x.replace(",", "")) for x in AMOUNT.findall(m.group("amount"))]
        lo = nums[0] if nums else 0
        hi = nums[1] if len(nums) > 1 else (0 if m.group("amount").startswith(("Over", "Spouse")) else lo)
        if m.group("amount").startswith(("Over", "Spouse")):
            lo, hi = nums[0] + 1, 0  # 0 = 상한 없음
        to_iso = lambda s: f"{s[6:10]}-{s[0:2]}-{s[3:5]}"
        rows.append({"owner": owner, "ticker": tickers[-1] if tickers else "", "asset": asset,
                     "asset_type": (m.group("type") or "").upper() or ("OP" if re.search(r"option", asset, re.I) else "ST" if tickers else ""), "tx": m.group("tx"), "trade_date": to_iso(m.group("date")),
                     "notified": to_iso(m.group("notified")), "amount_lo": lo, "amount_hi": hi})
    return rows


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 2017
    out_rows, scanned, empty = [], 0, 0
    for year in range(start, date.today().year + 1):
        filings = ptr_index(year)
        pdf_dir = OUT / "ptr" / str(year)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        fetched = 0
        cache_path = OUT / "parsed" / f"{year}.json"
        cache_path.parent.mkdir(exist_ok=True)
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        for f in filings:
            if f["doc_id"] in cache:
                rows = cache[f["doc_id"]]
                if rows is None:
                    scanned += 1
                out_rows += [{**f, **r} for r in rows or []]
                continue
            path = pdf_dir / f"{f['doc_id']}.pdf"
            if not path.exists():
                try:
                    path.write_bytes(get(PDF_URL.format(year=year, doc=f["doc_id"])))
                except Exception:
                    continue
                fetched += 1
                time.sleep(0.2)
            text = pdf_text(path)
            if len(text.strip()) < 50:
                scanned += 1
                cache[f["doc_id"]] = None  # 스캔본
                continue
            rows = parse(text)
            if not rows:
                empty += 1
            cache[f["doc_id"]] = rows
            out_rows += [{**f, **r} for r in rows]
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        print(f"{year}: 신고서 {len(filings)}건 (새로 받음 {fetched})", flush=True)
    out_rows.sort(key=lambda r: (r["filed"], r["doc_id"]))
    with (OUT / "house_trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows({k: r[k] for k in FIELDS} for r in out_rows)
    print(f"거래 {len(out_rows)}줄 · 스캔 신고서(건너뜀) {scanned} · 거래를 못 찾은 신고서 {empty}")


if __name__ == "__main__":
    main()
