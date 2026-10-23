"""미국 상장 종목의 한글명·별칭을 자동으로 만드는 파이프라인.

1) SEC 종목 목록 (나스닥·NYSE 전 종목, 무료)
2) Wikidata 한국어 이름·별칭 (CIK로 매칭, 무료) - 영어 라벨, 제품명 별칭 등이 섞여 있어 참고용으로만 씀
3) Claude가 Wikidata 참고 정보를 보고 최종 한글명·별칭을 정리
   (결과는 캐시에 저장해 재실행 시 새 종목만 호출)

실행:
    set SEC_USER_AGENT=지금이닷 you@example.com
    python build_ticker_names.py            # 전체
    python build_ticker_names.py --limit 50 # Claude 호출을 50종목으로 제한 (테스트용)
"""

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic
import requests

SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
EXCHANGES = {"Nasdaq", "NYSE"}

MODEL = "claude-opus-5-5"
BATCH_SIZE = 100

OUT_DIR = Path(__file__).parent / "output"
CACHE_PATH = OUT_DIR / "llm_cache.json"
RESULT_PATH = OUT_DIR / "ticker_names.json"

WIKIDATA_QUERY = """
SELECT ?cik ?ko ?alias WHERE {
  ?item wdt:P5531 ?cik ;
        rdfs:label ?ko .
  FILTER(LANG(?ko) = "ko")
  OPTIONAL { ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "ko") }
}
"""

SYSTEM_PROMPT = """너는 미국 주식 종목명을 한국 투자자용으로 표기하는 담당자다.
각 종목에 대해:
- name_ko: 한국 증권사 앱과 언론에서 실제로 쓰는 한글 이름. 널리 쓰이는 이름이 없으면 영문 회사명을 한글로 음차한다.
  'Inc.', 'Corp', 'Holdings', 'Class A' 같은 법인·주식 종류 표기는 빼되, 같은 회사의 다른 주식 종류를 구분해야 하면 남긴다.
- aliases_ko: 한국 투자자들이 실제로 부르는 줄임말·별칭만 넣는다 (예: 마이크로소프트 → 마소). 확실하지 않으면 빈 배열로 둔다. 지어내지 않는다.
일부 종목에는 위키데이터의 한국어 이름·별칭이 참고로 붙어 있다. 참고만 하고, 영어 표기, 제품·브랜드명, 옛 회사명, 오탈자는 버린다.
입력받은 모든 티커를 빠짐없이, 받은 티커 문자열 그대로 돌려준다."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "name_ko": {"type": "string"},
                    "aliases_ko": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ticker", "name_ko", "aliases_ko"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def fetch_sec_tickers(user_agent: str) -> dict[str, dict]:
    res = requests.get(SEC_URL, headers={"User-Agent": user_agent}, timeout=30)
    res.raise_for_status()
    data = res.json()
    col = {name: i for i, name in enumerate(data["fields"])}
    stocks = {}
    for row in data["data"]:
        if row[col["exchange"]] not in EXCHANGES:
            continue
        ticker = row[col["ticker"]]
        stocks[ticker] = {
            "ticker": ticker,
            "exchange": row[col["exchange"]],
            "cik": int(row[col["cik"]]),
            "name_en": row[col["name"]],
        }
    return stocks


def fetch_wikidata_names(user_agent: str) -> dict[int, dict]:
    res = requests.get(
        WIKIDATA_URL,
        params={"query": WIKIDATA_QUERY, "format": "json"},
        headers={"User-Agent": user_agent, "Accept": "application/sparql-results+json"},
        timeout=120,
    )
    res.raise_for_status()
    names: dict[int, dict] = {}
    for b in res.json()["results"]["bindings"]:
        try:
            cik = int(b["cik"]["value"])
        except ValueError:
            continue
        entry = names.setdefault(cik, {"name_ko": b["ko"]["value"], "aliases_ko": []})
        alias = b.get("alias", {}).get("value")
        if alias and alias not in entry["aliases_ko"]:
            entry["aliases_ko"].append(alias)
    return names


def describe(stock: dict, wiki: dict[int, dict]) -> str:
    line = f"{stock['ticker']}\t{stock['name_en']}"
    hint = wiki.get(stock["cik"])
    if hint:
        line += f"\t위키데이터: {hint['name_ko']}"
        if hint["aliases_ko"]:
            line += f" / 별칭: {', '.join(hint['aliases_ko'])}"
    return line


def ask_claude(client: anthropic.Anthropic, batch: list[dict], wiki: dict[int, dict]) -> list[dict]:
    lines = "\n".join(describe(s, wiki) for s in batch)
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": f"티커\t영문명\t(참고)\n{lines}"}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        print(f"  건너뜀 (stop_reason={response.stop_reason}), 다음 실행 때 다시 시도", file=sys.stderr)
        return []
    text = next(b.text for b in response.content if b.type == "text")
    asked = {s["ticker"] for s in batch}
    return [item for item in json.loads(text)["items"] if item["ticker"] in asked]


def load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Claude로 채울 종목 수 상한 (테스트용)")
    args = parser.parse_args()

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        sys.exit("SEC_USER_AGENT 환경변수를 '앱이름 연락처이메일' 형식으로 설정하세요 (SEC 요구사항).")
    OUT_DIR.mkdir(exist_ok=True)

    stocks = fetch_sec_tickers(user_agent)
    print(f"SEC 종목: {len(stocks)}개")
    wiki = fetch_wikidata_names(user_agent)
    print(f"Wikidata 한글명 (CIK 기준): {len(wiki)}개")

    cache = load_cache()
    todo = [s for t, s in sorted(stocks.items()) if t not in cache]
    if args.limit is not None:
        todo = todo[: args.limit]
    print(f"Claude로 정리할 종목: {len(todo)}개 (캐시 {len(cache)}개 재사용)")

    if todo:
        client = anthropic.Anthropic()
        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i : i + BATCH_SIZE]
            for item in ask_claude(client, batch, wiki):
                cache[item["ticker"]] = {"name_ko": item["name_ko"], "aliases_ko": item["aliases_ko"]}
            save_json(CACHE_PATH, cache)
            print(f"  {min(i + BATCH_SIZE, len(todo))}/{len(todo)} 완료")

    result = [
        {**s, **cache[t], "wikidata_hint": s["cik"] in wiki}
        for t, s in sorted(stocks.items())
        if t in cache
    ]
    save_json(RESULT_PATH, result)

    missing = len(stocks) - len(result)
    print(f"저장: {RESULT_PATH} ({len(result)}개, 아직 한글명 없음 {missing}개)")


if __name__ == "__main__":
    main()
