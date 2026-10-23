"""IEX 거래소 공개 데이터(HIST, DEEP 피드)에서 종목별 일별 종가를 뽑는다.

- 데이터: https://iextrading.com/api/1.0/hist (무료, 가입 불필요, T+1 공개, 최근 12개월 보관)
- 출처 표기 의무: "Data provided for free by IEX. By accessing or using IEX Historical Data,
  you agree to the IEX Historical Data Terms of Use."
- IEX 한 거래소의 체결만 담기므로 미국 통합 공식 종가와 다를 수 있다.

파일(하루 약 12GB, gzip)을 디스크에 저장하지 않고 내려받으면서 바로 파싱한다.

실행:
    python iex_closes.py                       # 가장 최근에 공개된 날짜
    python iex_closes.py 20261021              # 특정 날짜
    python iex_closes.py 20261021 --max-mb 300 # 앞부분 300MB만 (파서 확인용)
"""

import argparse
import csv
import gzip
import json
import struct
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HIST_LIST_API = "https://iextrading.com/api/1.0/hist"
HIST_API = "https://iextrading.com/api/1.0/hist?date={date}"
FEED = "DEEP"  # 체결 기록은 TOPS와 같고, 여러 피드 중 가장 먼저 공개됨
OUT_DIR = Path(__file__).parent / "output"

TRADE_REPORT = 0x54  # 'T'
OFFICIAL_PRICE = 0x58  # 'X'
EXTENDED_HOURS = 0x40  # Sale Condition Flags: 장 전·후 거래
IEX_TP_HEADER = 40
PRICE_SCALE = 10_000  # IEX 가격은 소수점 4자리 정수


class CountingReader:
    """다운로드한 압축 바이트 수를 세고, 상한에 닿으면 EOF처럼 동작한다."""

    def __init__(self, raw, max_bytes=None):
        self.raw, self.max_bytes, self.read_bytes = raw, max_bytes, 0

    def read(self, n=-1):
        if self.max_bytes is not None:
            left = self.max_bytes - self.read_bytes
            if left <= 0:
                return b""
            n = left if n < 0 else min(n, left)
        chunk = self.raw.read(n)
        self.read_bytes += len(chunk)
        return chunk


def latest_date() -> str:
    listing = json.load(urllib.request.urlopen(HIST_LIST_API, timeout=60))
    return max(d for d, files in listing.items() if any(f["feed"] == FEED for f in files))


def feed_link(date: str) -> str:
    files = json.load(urllib.request.urlopen(HIST_API.format(date=date), timeout=30))
    if not isinstance(files, list):
        sys.exit(f"{date} 파일이 아직 없습니다 (IEX는 다음 날 공개).")
    link = next((f["link"] for f in files if f["feed"] == FEED), None)
    if link is None:
        sys.exit(f"{date} {FEED} 파일이 아직 없습니다.")
    return link


def iter_packets(stream):
    """pcapng에서 Enhanced Packet Block의 패킷 데이터를 꺼낸다."""
    while True:
        header = stream.read(8)
        if len(header) < 8:
            return
        block_type, block_len = struct.unpack("<II", header)
        body = stream.read(block_len - 8)
        if len(body) < block_len - 8:
            return
        if block_type == 6:  # Enhanced Packet Block
            captured = struct.unpack_from("<I", body, 12)[0]
            yield memoryview(body)[20 : 20 + captured]


def iex_tp_payload(packet: memoryview) -> memoryview | None:
    """Ethernet → IPv4 → UDP 헤더를 건너뛰고 IEX-TP 세그먼트를 돌려준다."""
    if len(packet) < 14 + 20 + 8 + IEX_TP_HEADER:
        return None
    ip_header_len = (packet[14] & 0x0F) * 4
    return packet[14 + ip_header_len + 8 :]


def parse(date: str, max_bytes: int | None):
    resp = urllib.request.urlopen(feed_link(date), timeout=60)
    counter = CountingReader(resp, max_bytes)
    stream = gzip.GzipFile(fileobj=counter)

    last = {}  # symbol -> [price, ts_ns, volume, trades]
    official_close = {}
    started = time.time()
    try:
        for packet in iter_packets(stream):
            seg = iex_tp_payload(packet)
            if seg is None:
                continue
            count = struct.unpack_from("<H", seg, 14)[0]
            pos = IEX_TP_HEADER
            for _ in range(count):
                length = seg[pos] | (seg[pos + 1] << 8)
                msg_type = seg[pos + 2]
                if msg_type == TRADE_REPORT and not seg[pos + 3] & EXTENDED_HOURS:
                    ts, sym, size, price = struct.unpack_from("<q8sIq", seg, pos + 4)
                    entry = last.get(sym)
                    if entry is None:
                        last[sym] = [price, ts, size, 1]
                    else:
                        if ts >= entry[1]:
                            entry[0], entry[1] = price, ts
                        entry[2] += size
                        entry[3] += 1
                elif msg_type == OFFICIAL_PRICE and seg[pos + 3] == ord("M"):
                    _, sym, price = struct.unpack_from("<q8sq", seg, pos + 4)
                    official_close[sym] = price
                pos += 2 + length
    except (EOFError, gzip.BadGzipFile):
        pass  # --max-mb로 중간에 끊은 경우
    elapsed = time.time() - started
    return last, official_close, counter.read_bytes, elapsed


def write_csv(date: str, last: dict, official_close: dict, partial: bool) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"iex_{date}{'_partial' if partial else ''}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "iex_last_price", "iex_official_close", "last_trade_utc", "volume", "trades"])
        for sym, (price, ts, volume, trades) in sorted(last.items()):
            close = official_close.get(sym)
            w.writerow([
                sym.decode().rstrip(),
                price / PRICE_SCALE,
                close / PRICE_SCALE if close is not None else "",
                datetime.fromtimestamp(ts / 1e9, timezone.utc).strftime("%H:%M:%S"),
                volume,
                trades,
            ])
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", help="YYYYMMDD (생략하면 가장 최근 날짜)")
    parser.add_argument("--max-mb", type=int, help="압축 기준 앞부분만 읽기 (테스트용)")
    args = parser.parse_args()

    date = args.date or latest_date()
    print(f"대상 날짜: {date}")
    max_bytes = args.max_mb * 1_000_000 if args.max_mb else None
    last, official_close, read_bytes, elapsed = parse(date, max_bytes)
    path = write_csv(date, last, official_close, partial=max_bytes is not None)
    mb = read_bytes / 1e6
    print(f"읽은 양 {mb:,.0f}MB / {elapsed:,.0f}초 ({mb / max(elapsed, 1):,.1f}MB/s)")
    print(f"종목 {len(last):,}개 → {path}")


if __name__ == "__main__":
    main()
