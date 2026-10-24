"""앱의 한글 종목 검색용 임시 목록: Wikidata 한국어 이름·별칭 중 한글이 들어간 것만.

build_ticker_names.py(Claude로 전 종목 정리)를 돌리기 전까지 쓰는 무료 버전이다.
출력: data/names/ticker_names_ko.json  {"NVDA": ["엔비디아", ...], ...}  (첫 번째가 대표 이름)

    set SEC_USER_AGENT=지금이닷 연락처이메일
    python export_wikidata_names.py
"""

import json
import os
import re
import sys
from pathlib import Path

from build_ticker_names import fetch_sec_tickers, fetch_wikidata_names

OUT = Path(__file__).resolve().parents[2] / "data" / "names" / "ticker_names_ko.json"
HANGUL = re.compile(r"[가-힣]")

# 한국 투자자가 많이 찾지만 Wikidata에 없거나 흔한 이름이 빠진 종목·ETF. 앞에 둔 이름이 대표 이름.
CURATED = {
    "NVDA": ["엔비디아"], "AVGO": ["브로드컴"], "GOOGL": ["알파벳 A", "구글"], "GOOG": ["알파벳 C", "구글"],
    "TSM": ["TSMC", "대만 반도체"], "KO": ["코카콜라"], "PLTR": ["팔란티어"], "AMD": ["AMD"],
    "INTC": ["인텔"], "MU": ["마이크론"], "QCOM": ["퀄컴"], "ARM": ["ARM", "암 홀딩스"], "ASML": ["ASML"],
    "COST": ["코스트코"], "WMT": ["월마트"], "JPM": ["JP모건"], "BRK.B": ["버크셔 해서웨이 B"],
    "LLY": ["일라이 릴리"], "NVO": ["노보 노디스크"], "UNH": ["유나이티드헬스"], "ORCL": ["오라클"],
    "CRM": ["세일즈포스"], "DIS": ["디즈니"], "NKE": ["나이키"], "SBUX": ["스타벅스"], "MCD": ["맥도날드"],
    "PEP": ["펩시코"], "V": ["비자"], "MA": ["마스터카드"], "BAC": ["뱅크오브아메리카"], "XOM": ["엑슨모빌"],
    "CVX": ["셰브론"], "SMCI": ["슈퍼마이크로"], "CRWD": ["크라우드스트라이크"], "SNOW": ["스노우플레이크"],
    "UBER": ["우버"], "ABNB": ["에어비앤비"], "SHOP": ["쇼피파이"], "BABA": ["알리바바"], "COIN": ["코인베이스"],
    "MSTR": ["스트래티지", "마이크로스트래티지"], "IONQ": ["아이온큐"], "SOFI": ["소파이"], "RKLB": ["로켓랩"],
    "O": ["리얼티인컴"], "SPY": ["S&P500 ETF (SPY)"], "VOO": ["뱅가드 S&P500 ETF"], "QQQ": ["나스닥100 ETF (QQQ)"],
    "QQQM": ["나스닥100 ETF (QQQM)"], "TQQQ": ["나스닥100 3배 레버리지"], "SOXL": ["반도체 3배 레버리지"],
    "SCHD": ["슈드", "미국 배당 다우존스 ETF"], "JEPI": ["제피"], "TLT": ["미국 장기채 ETF"],
}


def main() -> None:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        sys.exit("SEC_USER_AGENT 환경변수를 '앱이름 연락처이메일' 형식으로 설정하세요.")
    stocks = fetch_sec_tickers(user_agent)
    wiki = fetch_wikidata_names(user_agent)
    names = {}
    for ticker, s in sorted(stocks.items()):
        hint = wiki.get(s["cik"])
        if not hint:
            continue
        found = [n for n in [hint["name_ko"], *hint["aliases_ko"]] if HANGUL.search(n)]
        if found:
            names[ticker.replace("-", ".")] = found  # 앱·IEX는 BRK.B 형식
    for ticker, curated in CURATED.items():
        names[ticker] = list(dict.fromkeys([*curated, *names.get(ticker, [])]))
    names = {t: list(dict.fromkeys(n)) for t, n in sorted(names.items())}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"한글 이름 {len(names)}종목 → {OUT} ({OUT.stat().st_size // 1024}KB)")


if __name__ == "__main__":
    main()
