"""IEX HIST에 공개됐지만 out-dir에 아직 없는 날짜를 최신순으로 N개, JSON 배열로 출력한다.

    python missing_dates.py 20 data/iex   # ["20261021", "20261020", ...]
"""

import json
import sys
import urllib.request
from pathlib import Path

from iex_closes import FEED, HIST_LIST_API


def main() -> None:
    count, out_dir = int(sys.argv[1]), Path(sys.argv[2])
    listing = json.load(urllib.request.urlopen(HIST_LIST_API, timeout=60))
    available = sorted((d for d, files in listing.items() if any(f["feed"] == FEED for f in files)), reverse=True)
    saved = {p.stem.removeprefix("iex_") for p in out_dir.glob("iex_????????.csv")}
    print(json.dumps([d for d in available if d not in saved][:count]))


if __name__ == "__main__":
    main()
