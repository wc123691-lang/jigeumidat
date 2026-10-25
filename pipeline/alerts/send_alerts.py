"""내 신호 푸시 알림 (매일 저녁, iex_daily.yml에서 신호 계산 다음에 실행).

앱이 Supabase에 올린 사용자별 알림 정보(알림 받을 종목, 그 종목들의 내 신호 설정, 푸시 토큰)로
오늘의 내 신호를 계산해, 매수·매도 기준을 '새로' 넘은 종목을 사용자마다 한 번에 묶어 Expo 푸시로 보낸다.
- 처음 보는 종목은 상태만 기록한다 (알림을 켠 날 이미 기준 위에 있던 종목까지 한꺼번에 울리지 않게).
- 같은 신호 날짜로 다시 돌면 보내지 않는다 (휴장일·재실행).
내 신호 공식은 앱(src/signals.ts mySignal, rsiScore)과 같다: 값이 있는 신호끼리 비중으로 가중 평균.

공개 저장소이므로 로그에는 개수만 남기고 사용자 정보는 찍지 않는다.
비밀 키는 GitHub Secrets(SUPABASE_URL, SUPABASE_SECRET_KEY)로만 받는다. 없으면 아무것도 하지 않고 끝낸다.

    python send_alerts.py            # 보내기
    python send_alerts.py --dry-run  # 계산만 (보내지도, 상태를 바꾸지도 않음)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = ROOT / "data" / "signals"
EXPO_PUSH = "https://exp.host/--/api/v2/push/send"
PAGE = 1000
FIELD_TO_KEY = {"s_rsi": "rsi", "s_bb": "bb", "s_gap": "gap", "s_trend": "trend", "s_dd": "dd", "s_ins": "ins",
                "s_fg": "fg", "s_vix": "vix", "s_fx": "fx", "s_spx": "spx"}
NAME = {"buy": "매수", "sell": "매도"}


def clamp(x: float) -> float:
    return max(-100.0, min(100.0, x))


def rsi_score(r: float, buy: float, sell: float) -> float:
    return clamp(((buy + sell) / 2 - r) / ((sell - buy) / 2) * 100)


def my_signal(components: dict, settings: dict) -> float | None:
    weights = settings.get("weights", {})
    used = {k: w for k, w in weights.items() if k in components and w > 0}
    total = sum(used.values())
    return sum(components[k] * w for k, w in used.items()) / total if total else None


def components_for(symbol: str, scores: dict, market: dict, settings: dict) -> dict | None:
    row = scores["data"].get(symbol)
    if row is None:
        return None
    out = {}
    for field, value in zip(scores["fields"], row):
        if value is None:
            continue
        if field in FIELD_TO_KEY:
            out[FIELD_TO_KEY[field]] = value
        if field == "rsi14":  # 사용자의 RSI 기준선으로 다시 계산 (앱과 같게)
            out["rsi"] = rsi_score(value, settings.get("rsiBuy", 30), settings.get("rsiSell", 70))
    for field, key in FIELD_TO_KEY.items():
        if isinstance(market.get(field), (int, float)):
            out[key] = market[field]
    return out


class Supabase:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/").removesuffix("/rest/v1") + "/rest/v1/"
        self.key = key

    def call(self, method: str, path: str, body=None, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"apikey": self.key, "Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode()
        return json.loads(text) if text else None

    def all(self, table: str, query: str) -> list[dict]:
        rows, offset = [], 0
        while True:
            page = self.call("GET", f"{table}?{query}&limit={PAGE}&offset={offset}")
            rows += page
            if len(page) < PAGE:
                return rows
            offset += PAGE


def send_push(messages: list[dict]) -> list[str]:
    """Expo 푸시 (한 번에 100개씩). 더 이상 유효하지 않은 토큰 목록을 돌려준다."""
    dead = []
    for i in range(0, len(messages), 100):
        batch = messages[i:i + 100]
        req = urllib.request.Request(EXPO_PUSH, data=json.dumps(batch).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            tickets = json.loads(r.read().decode()).get("data", [])
        for msg, ticket in zip(batch, tickets):
            if ticket.get("status") == "error" and ticket.get("details", {}).get("error") == "DeviceNotRegistered":
                dead.append(msg["to"])
    return dead


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SECRET_KEY가 없어 알림을 건너뜁니다.")
        return

    scores = json.loads((SIGNALS / "scores_latest.json").read_text(encoding="utf-8"))
    market = json.loads((SIGNALS / "market_latest.json").read_text(encoding="utf-8"))
    d = scores["date"]
    today = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    db = Supabase(url, key)
    profiles = db.all("alert_profiles", "select=user_id,push_token,base_settings,symbols&enabled=eq.true")
    state = {(r["user_id"], r["symbol"]): r for r in db.all("alert_state", "select=user_id,symbol,zone,signal_date")}

    messages, updates, first_seen = [], [], 0
    for p in profiles:
        hits = []
        for target in p["symbols"] or []:
            symbol = target["symbol"]
            settings = target.get("settings") or p["base_settings"]
            comps = components_for(symbol, scores, market, settings)
            score = my_signal(comps, settings) if comps else None
            if score is None:
                continue
            zone = "buy" if score >= settings["buy"] else "sell" if score <= settings["sell"] else "neutral"
            prev = state.get((p["user_id"], symbol))
            if prev and prev["signal_date"] == today:
                continue  # 오늘 신호로 이미 처리함
            if prev is None:
                first_seen += 1
            elif zone != prev["zone"] and zone in NAME:
                hits.append(f"{symbol} {NAME[zone]} 기준 충족 ({'+' if score > 0 else '−'}{abs(round(score))})")
            updates.append({"user_id": p["user_id"], "symbol": symbol, "zone": zone, "score": round(score, 1), "signal_date": today})
        if hits and p.get("push_token"):
            messages.append({"to": p["push_token"], "title": "지금이닷 · 내 신호", "body": " · ".join(hits),
                             "sound": "default", "channelId": "signals", "data": {"screen": "Journal"}})

    print(f"{today} 알림 대상 {len(profiles)}명 · 종목 상태 {len(updates)}건(처음 {first_seen}) · 보낼 알림 {len(messages)}건")
    if args.dry_run:
        return
    dead = send_push(messages) if messages else []
    for i in range(0, len(updates), 500):
        db.call("POST", "alert_state?on_conflict=user_id,symbol", updates[i:i + 500],
                {"Prefer": "resolution=merge-duplicates,return=minimal"})
    for token in dead:  # 앱을 지웠거나 토큰이 바뀐 기기
        db.call("PATCH", "alert_profiles?push_token=eq." + urllib.parse.quote(token), {"push_token": None}, {"Prefer": "return=minimal"})
    print(f"보냄 {len(messages)}건 · 만료된 토큰 정리 {len(dead)}건")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        # 응답 본문에 사용자 정보가 섞일 수 있어 상태 코드만 남긴다
        sys.exit(f"Supabase/Expo 요청 실패: HTTP {e.code}")
