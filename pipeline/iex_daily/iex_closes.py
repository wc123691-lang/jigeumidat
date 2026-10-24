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
    python iex_closes.py --out-dir data/iex --wait-new 4
        # out-dir에 저장된 마지막 날짜보다 새 파일이 나올 때까지 최대 4시간 기다렸다가 처리
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
FALLBACK_FEED = "TOPS"  # DEEP이 없는 날(2017-05 이전 등)
OUT_DIR = Path(__file__).parent / "output"

TRADE_REPORT = 0x54  # 'T'
OFFICIAL_PRICE = 0x58  # 'X'
EXTENDED_HOURS = 0x40  # Sale Condition Flags: 장 전·후 거래
IEX_TP_HEADER = 40
PRICE_SCALE = 10_000  # IEX 가격은 소수점 4자리 정수
POLL_SECONDS = 600


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


def wait_for_new_date(after: str, hours: float) -> str | None:
    deadline = time.time() + hours * 3600
    while True:
        date = latest_date()
        if date > after:
            return date
        if time.time() + POLL_SECONDS > deadline:
            return None
        print(f"아직 {after} 이후 파일 없음, {POLL_SECONDS // 60}분 뒤 다시 확인", flush=True)
        time.sleep(POLL_SECONDS)


def last_saved_date(out_dir: Path) -> str:
    dates = [p.stem.removeprefix("iex_") for p in out_dir.glob("iex_????????.csv")]
    return max(dates, default="00000000")


def feed_file(date: str) -> dict:
    files = json.load(urllib.request.urlopen(HIST_API.format(date=date), timeout=30))
    if not isinstance(files, list):
        sys.exit(f"{date} 파일이 아직 없습니다 (IEX는 다음 날 공개).")
    by_feed = {f["feed"]: f for f in files}
    file = by_feed.get(FEED) or by_feed.get(FALLBACK_FEED)
    if file is None:
        sys.exit(f"{date} {FEED}/{FALLBACK_FEED} 파일이 아직 없습니다.")
    return file


def has_trades(date: str, files: list) -> bool:
    """평일이고 체결이 담긴 피드가 있는 날 (IEX 목록에 섞인 주말 테스트 날짜 제외)."""
    weekday = datetime.strptime(date, "%Y%m%d").weekday()
    return weekday < 5 and any(f["feed"] in (FEED, FALLBACK_FEED) for f in files)


def iter_packets(stream):
    """패킷 데이터를 꺼낸다. 2017-08 이후 파일은 pcapng, 그 전 TOPS 파일은 예전 pcap 형식."""
    magic = stream.read(4)
    if magic == b"\xd4\xc3\xb2\xa1":  # pcap (little-endian)
        stream.read(20)  # 나머지 전역 헤더
        while True:
            header = stream.read(16)
            if len(header) < 16:
                return
            captured = struct.unpack_from("<I", header, 8)[0]
            data = stream.read(captured)
            if len(data) < captured:
                return
            yield memoryview(data)
    # pcapng: 첫 블록(Section Header Block)의 타입 4바이트를 이미 읽었으므로 나머지를 건너뛴다
    block_len = struct.unpack("<I", stream.read(4))[0]
    stream.read(block_len - 8)
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
    file = feed_file(date)
    resp = urllib.request.urlopen(file["link"], timeout=60)
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
        if max_bytes is None:
            raise  # 전체 실행에서 파일이 중간에 끝났다면 다운로드가 끊긴 것
    if max_bytes is None and counter.read_bytes != int(file["size"]):
        raise RuntimeError(f"다운로드가 중간에 끊김: {counter.read_bytes:,} / {int(file['size']):,} bytes")
    elapsed = time.time() - started
    return last, official_close, counter.read_bytes, elapsed


def write_csv(out_dir: Path, date: str, last: dict, official_close: dict, partial: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"iex_{date}{'_partial' if partial else ''}.csv"
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
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--wait-new", type=float, metavar="HOURS",
                        help="out-dir의 마지막 날짜보다 새 파일이 나올 때까지 기다릴 최대 시간")
    args = parser.parse_args()

    if args.date:
        date = args.date
    elif args.wait_new is not None:
        date = wait_for_new_date(last_saved_date(args.out_dir), args.wait_new)
        if date is None:
            print("새 파일이 없습니다 (휴장일이거나 IEX 공개 지연). 종료합니다.")
            return
    else:
        date = latest_date()
    print(f"대상 날짜: {date}")
    max_bytes = args.max_mb * 1_000_000 if args.max_mb else None
    last, official_close, read_bytes, elapsed = parse(date, max_bytes)
    path = write_csv(args.out_dir, date, last, official_close, partial=max_bytes is not None)
    mb = read_bytes / 1e6
    print(f"읽은 양 {mb:,.0f}MB / {elapsed:,.0f}초 ({mb / max(elapsed, 1):,.1f}MB/s)")
    print(f"종목 {len(last):,}개 → {path}")


if __name__ == "__main__":
    main()
