"""내 신호 푸시 알림 (매일 저녁, iex_daily.yml에서 신호 계산 다음에 실행).

앱이 Supabase에 올린 사용자별 알림 정보(알림 받을 종목, 그 종목들의 내 신호 설정, 푸시 토큰)로
오늘의 내 신호를 계산해, 매수·매도 기준을 '새로' 넘은 종목을 사용자마다 한 번에 묶어 Expo 푸시로 보낸다.
- 처음 보는 종목은 상태만 기록한다 (알림을 켠 날 이미 기준 위에 있던 종목까지 한꺼번에 울리지 않게).
- 같은 신호 날짜로 다시 돌면 보내지 않는다 (휴장일·재실행).
내 신호 공식은 앱(src/signals.ts mySignal, rsiScore)과 같다: 값이 있는 신호끼리 비중으로 가중 평균.
- 분할 계획(설정의 "split": 점수대별 사고팔 비중)이 있으면 알림에 '계획 1단계 30% 분할매수'처럼 붙이고,
  이미 기준을 넘은 상태에서 다음 단계를 새로 넘은 날도 알린다 (어제 점수와 비교, 앱 src/signals.ts splitText와 같은 규칙).
- 알림에 그 쪽으로 가장 크게 기여한 신호를 붙이고, 지난 실행보다 BIG_MOVE점 넘게 움직였으면 하루 변화도 붙인다 (앱 BIG_MOVE와 같은 값).

내부자 거래 알림도 같이 보낸다 (별도 푸시): 사용자가 지켜보는 종목(보유·전용 신호·시뮬레이션해 본 종목)에
지난 실행 뒤 새로 공시된 내부자 장내 매수를 묶어 알린다. 매도는 사용자가 켰을 때만, 계획 매도(10b5-1)는 빼고.
- 종목 항목의 "signal"(내 신호 알림 대상)·"insider"("buy" / "all" / 없음)로 고른다. 예전 앱이 올린 항목은 signal만.
- 사용자마다 '어디까지 알렸나'(마지막 공시일)는 alert_state의 '__insider__' 줄에 둔다 (표를 새로 만들지 않으려고).
  처음에는 기준만 기록하고 알리지 않는다.

시장 알림도 보낸다 (별도 푸시): 공포·탐욕 지수가 극단적 공포(25 미만)나 극단적 탐욕(75 초과) 구간에 '새로' 들어간 날.
- 사용자가 끄면 base_settings의 "marketAlert"가 false다 (예전 앱은 이 값이 없어 켠 것으로 본다).
- 구간 경계에서 매일 들락날락하며 여러 번 울리지 않게, 나올 때는 10점 여유를 둔다 (공포는 35 이상이 돼야 벗어남).
- 사용자마다 지금 구간은 alert_state의 '__market__' 줄에 둔다 (공포 = zone 'buy', 탐욕 = 'sell'). 처음에는 기록만 한다.

하원의원 거래 알림도 보낸다 (별도 푸시): 사용자가 의원 상세 화면에서 켠 의원(base_settings의 "followMembers" id 목록)에
지난 실행 뒤 새로 공시된 거래가 있으면 묶어 알린다. 공시는 data/congress/members/<id>.json (PC 정오 작업이 매일 갱신).
- 사용자마다 '어디까지 알렸나'(마지막 공시일)는 alert_state의 '__congress__' 줄에 둔다. 처음에는 기준만 기록한다.

매일 업데이트 알림은 따로 돈다 (--daily-update, daily_update.yml이 밤 10시 KST에 실행): 오늘 신호가 새로 계산된 날
푸시 토큰이 있는 모든 사용자에게 같은 문구를 보낸다. '알림 받기'(enabled)와 상관없고, 끈 사용자는 base_settings의
"dailyUpdate"가 false다 (예전 앱은 이 값이 없어 켠 것으로 본다).
- 신호 날짜가 어제(UTC)가 아니면(휴장일, 데이터가 아직 안 나옴) 보내지 않는다.
- 금요일 장 데이터(한국 토요일 밤)는 주말이라 바로 거래할 수 없으니 월요일 장을 준비하자는 문구로 보낸다.

공개 저장소이므로 로그에는 개수만 남기고 사용자 정보는 찍지 않는다.
비밀 키는 GitHub Secrets(SUPABASE_URL, SUPABASE_SECRET_KEY)로만 받는다. 없으면 아무것도 하지 않고 끝낸다.

    python send_alerts.py            # 보내기
    python send_alerts.py --dry-run  # 계산만 (보내지도, 상태를 바꾸지도 않음)
    python send_alerts.py --daily-update [--dry-run]  # 매일 업데이트 알림만
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = ROOT / "data" / "signals"
INSIDER = ROOT / "data" / "insider" / "by_ticker"
INSIDER_KEY = "__insider__"
MARKET_KEY = "__market__"
CONGRESS_KEY = "__congress__"
CONGRESS = ROOT / "data" / "congress" / "members"
TX_WORD = {"P": "매수", "S": "매도", "S (partial)": "일부 매도", "E": "교환"}
FEAR, GREED, HYSTERESIS = 25, 75, 10  # 앱의 '극단적 공포'·'극단적 탐욕' 경계와 같다
EXPO_PUSH = "https://exp.host/--/api/v2/push/send"
PAGE = 1000
FIELD_TO_KEY = {"s_rsi": "rsi", "s_bb": "bb", "s_gap": "gap", "s_trend": "trend", "s_dd": "dd", "s_ins": "ins",
                "s_fg": "fg", "s_vix": "vix", "s_fx": "fx", "s_spx": "spx"}
NAME = {"buy": "매수", "sell": "매도"}
BIG_MOVE = 15  # 앱 src/signals.ts BIG_MOVE와 같게
LABEL = {"fg": "공포·탐욕 지수", "vix": "시장 변동성", "fx": "원/달러 환율", "spx": "S&P500 200일선", "rsi": "RSI",
         "bb": "볼린저밴드 위치", "gap": "200일선 아래 괴리", "trend": "200일선 위 추세", "ins": "내부자 매수", "dd": "52주 고점 대비 하락"}


def clamp(x: float) -> float:
    return max(-100.0, min(100.0, x))


def rsi_score(r: float, buy: float, sell: float) -> float:
    return clamp(((buy + sell) / 2 - r) / ((sell - buy) / 2) * 100)


def my_signal(components: dict, settings: dict) -> float | None:
    weights = settings.get("weights", {})
    used = {k: w for k, w in weights.items() if k in components and w > 0}
    total = sum(used.values())
    return sum(components[k] * w for k, w in used.items()) / total if total else None


def top_driver(components: dict, settings: dict, side: str) -> str | None:
    """그 쪽(매수 = +, 매도 = −)으로 가장 크게 기여한 신호 이름"""
    weights = settings.get("weights", {})
    used = {k: w for k, w in weights.items() if k in components and w > 0}
    total = sum(used.values())
    if not total:
        return None
    contrib = {k: components[k] * w / total for k, w in used.items()}
    k = max(contrib, key=contrib.get) if side == "buy" else min(contrib, key=contrib.get)
    return LABEL.get(k) if (contrib[k] > 0) == (side == "buy") else None


def split_steps(settings: dict, side: str) -> list[dict]:
    steps = (settings.get("split") or {}).get(side) or []
    return sorted(steps, key=lambda st: st["at"], reverse=side == "sell")  # 1단계부터


def split_reached(settings: dict, side: str, score: float) -> int:
    return sum(1 for st in split_steps(settings, side) if (score >= st["at"] if side == "buy" else score <= st["at"]))


def split_text(settings: dict, side: str, score: float, start: int = 0) -> str | None:
    """새로 넘은 단계(start 다음부터)의 비중 합: '2단계 30% 분할매수'. 없으면 None."""
    steps, n = split_steps(settings, side), split_reached(settings, side, score)
    if n <= start:
        return None
    pct = sum(st["pct"] for st in steps[start:n])
    return f"{f'{start + 1}~{n}단계' if n - start > 1 else f'{n}단계'} {pct:g}% 분할{NAME[side]}"


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


def insider_file(ticker: str) -> Path:
    """윈도우 예약 이름 티커(CON 등)는 _CON.json (pipeline/insider/history.py와 같은 규칙)."""
    reserved = ticker in ("CON", "PRN", "AUX", "NUL") or (len(ticker) == 4 and ticker[:3] in ("COM", "LPT") and ticker[3].isdigit())
    return INSIDER / (("_" if reserved else "") + ticker + ".json")


def market_zone(fg: float, prev: str | None) -> str:
    """공포 = 'buy', 탐욕 = 'sell', 그 사이 = 'neutral'. 이미 구간 안이면 HYSTERESIS만큼 벗어나야 나온다."""
    if fg < FEAR or (prev == "buy" and fg < FEAR + HYSTERESIS):
        return "buy"
    if fg > GREED or (prev == "sell" and fg > GREED - HYSTERESIS):
        return "sell"
    return "neutral"


def market_message(zone: str, fg: float) -> str:
    if zone == "buy":
        return f"시장이 극단적 공포 구간에 들어왔어요 (공포·탐욕 {round(fg)}). 내 종목의 신호가 매수 기준에 가까워졌는지 확인해 보세요."
    return f"시장이 극단적 탐욕 구간에 들어왔어요 (공포·탐욕 {round(fg)}). 들뜬 시장이에요, 매도 기준과 처음 정한 약속을 확인해 보세요."


def congress_lines(name: str, trades: list, after: str) -> list[str]:
    """after(공시일) 뒤 새로 공시된 거래 요약. trades: members/<id>.json 형식 [티커, 유형, 소유자, 거래종류, 거래일, 공시일, 하한, 상한, 자산, 종가]"""
    new = [t for t in trades if t[5] > after]
    if not new:
        return []
    by = {}
    for t in new:
        by.setdefault((t[3], t[0]), 0)
        by[(t[3], t[0])] += 1
    parts = [f"{tick} {TX_WORD.get(tx, tx)}" + (f" {n}건" if n > 1 else "") for (tx, tick), n in list(by.items())[:4]]
    more = len(by) - 4
    return [f"{name}: " + ", ".join(parts) + (f" 외 {more}종목" if more > 0 else "")]


def compact_usd(v: float) -> str:
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K" if v >= 1e3 else f"${v:.0f}"


def insider_hits(rows: list, after: str, mode: str) -> list[str]:
    """after(공시일) 뒤 새로 공시된 장내 매수(그리고 mode == 'all'이면 계획 매도가 아닌 매도) 요약.
    rows: by_ticker 형식 [거래일, 코드, 이름, 관계, 직책, 주식수, 가격, 금액, 거래 후 보유, 10b5-1, 공시일]"""
    out = []
    for code, word in (("P", "매수"), ("S", "매도")):
        if code == "S" and mode != "all":
            continue
        new = [r for r in rows if r[1] == code and r[10] > after and not (code == "S" and r[9])]
        if new:
            out.append((word, len({r[2] for r in new}), sum(r[7] for r in new)))
    return [f"내부자 {w} {n}명 {compact_usd(v)}" for w, n, v in out]


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


def daily_update(db: Supabase, dry_run: bool) -> None:
    d = json.loads((SIGNALS / "scores_latest.json").read_text(encoding="utf-8"))["date"]
    expected = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    if d != expected:
        print(f"신호 날짜 {d}가 어제({expected})가 아니라 매일 업데이트 알림을 건너뜁니다 (휴장일이거나 데이터가 아직 없음).")
        return
    profiles = db.all("alert_profiles", "select=push_token,base_settings&push_token=not.is.null")
    friday = datetime.strptime(d, "%Y%m%d").weekday() == 4
    body = ("금요일 미국장까지 반영한 신호가 업데이트됐어요. 주말 동안 내 종목 신호를 살펴보고 다음 주 장을 준비해 보세요." if friday
            else "오늘의 미국주식 신호가 업데이트됐어요. 내 종목 신호를 확인해 보세요.")
    messages = [{"to": p["push_token"], "title": "지금이닷", "body": body,
                 "sound": "default", "channelId": "signals", "data": {"screen": "Home"}}
                for p in profiles if (p["base_settings"] or {}).get("dailyUpdate") is not False]
    print(f"{d} 매일 업데이트 알림: 토큰 {len(profiles)}명 중 {len(messages)}건")
    if dry_run or not messages:
        return
    dead = send_push(messages)
    for token in dead:
        db.call("PATCH", "alert_profiles?push_token=eq." + urllib.parse.quote(token), {"push_token": None}, {"Prefer": "return=minimal"})
    print(f"보냄 {len(messages)}건 · 만료된 토큰 정리 {len(dead)}건")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--daily-update", action="store_true")
    args = parser.parse_args()
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SECRET_KEY가 없어 알림을 건너뜁니다.")
        return
    if args.daily_update:
        daily_update(Supabase(url, key), args.dry_run)
        return

    scores = json.loads((SIGNALS / "scores_latest.json").read_text(encoding="utf-8"))
    market = json.loads((SIGNALS / "market_latest.json").read_text(encoding="utf-8"))
    d = scores["date"]
    today = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    db = Supabase(url, key)
    profiles = db.all("alert_profiles", "select=user_id,push_token,base_settings,symbols&enabled=eq.true")
    state = {(r["user_id"], r["symbol"]): r for r in db.all("alert_state", "select=user_id,symbol,zone,score,signal_date")}

    messages, updates, first_seen = [], [], 0
    for p in profiles:
        hits = []
        for target in p["symbols"] or []:
            if target.get("signal") is False:
                continue  # 시뮬레이션만 해 본 종목 등: 내부자 알림만
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
            shown = f"{'+' if score > 0 else '−'}{abs(round(score))}"
            if prev and prev.get("score") is not None and abs(score - prev["score"]) >= BIG_MOVE:
                d = score - prev["score"]
                shown += f", 하루 {'+' if d > 0 else '−'}{abs(round(d))}"
            driver = top_driver(comps, settings, zone) if zone in NAME else None
            why = f" · {driver} 영향이 가장 커요" if driver else ""
            if prev is None:
                first_seen += 1
            elif zone != prev["zone"] and zone in NAME:
                plan = split_text(settings, zone, score)
                hits.append(f"{symbol} {NAME[zone]} 기준 충족 ({shown}){why}" + (f" · 계획 {plan}" if plan else ""))
            elif zone == prev["zone"] and zone in NAME and prev.get("score") is not None:
                # 이미 기준을 넘은 상태에서 분할 계획의 다음 단계를 새로 넘은 날
                plan = split_text(settings, zone, score, split_reached(settings, zone, prev["score"]))
                if plan:
                    hits.append(f"{symbol} {NAME[zone]} 다음 단계 ({shown}){why} · 계획 {plan}")
            updates.append({"user_id": p["user_id"], "symbol": symbol, "zone": zone, "score": round(score, 1), "signal_date": today})
        if hits and p.get("push_token"):
            messages.append({"to": p["push_token"], "title": "지금이닷 · 내 신호", "body": " · ".join(hits),
                             "sound": "default", "channelId": "signals", "data": {"screen": "Journal"}})

    # 내부자 거래: 지켜보는 종목에 지난번 뒤 새로 공시된 거래
    insider_cache: dict[str, list] = {}
    insider_messages = 0
    for p in profiles:
        watched = [(t["symbol"], t["insider"]) for t in p["symbols"] or [] if t.get("insider") in ("buy", "all")]
        if not watched:
            continue
        lines, newest = [], ""
        for symbol, mode in watched:
            if symbol not in insider_cache:
                path = insider_file(symbol)
                insider_cache[symbol] = json.loads(path.read_text(encoding="utf-8"))["rows"] if path.exists() else []
            rows = insider_cache[symbol]
            newest = max([newest, *(r[10] for r in rows)])
            prev = state.get((p["user_id"], INSIDER_KEY))
            if prev:
                lines += [f"{symbol} {h}" for h in insider_hits(rows, prev["signal_date"], mode)]
        if not newest:
            continue
        prev = state.get((p["user_id"], INSIDER_KEY))
        if prev and prev["signal_date"] >= newest:
            continue  # 새 공시 없음
        if prev is None:
            first_seen += 1  # 처음: 기준만 기록
        updates.append({"user_id": p["user_id"], "symbol": INSIDER_KEY, "zone": "neutral", "score": None, "signal_date": newest})
        if lines and p.get("push_token"):
            messages.append({"to": p["push_token"], "title": "지금이닷 · 내부자 거래", "body": " · ".join(lines),
                             "sound": "default", "channelId": "signals", "data": {"screen": "Insider"}})
            insider_messages += 1

    # 시장: 공포·탐욕 지수가 극단적 구간에 새로 들어간 날 (모든 사용자에게 같은 값)
    market_messages = 0
    fg = market.get("fear_greed")
    if isinstance(fg, (int, float)):
        for p in profiles:
            if (p["base_settings"] or {}).get("marketAlert") is False:
                continue
            prev = state.get((p["user_id"], MARKET_KEY))
            if prev and prev["signal_date"] == today:
                continue
            zone = market_zone(fg, prev["zone"] if prev else None)
            if prev is None:
                first_seen += 1  # 처음: 지금 구간만 기록
            elif zone != prev["zone"] and zone != "neutral" and p.get("push_token"):
                messages.append({"to": p["push_token"], "title": "지금이닷 · 시장", "body": market_message(zone, fg),
                                 "sound": "default", "channelId": "signals", "data": {"screen": "MarketCheck"}})
                market_messages += 1
            updates.append({"user_id": p["user_id"], "symbol": MARKET_KEY, "zone": zone, "score": round(fg, 1), "signal_date": today})

    # 하원의원: 사용자가 켠 의원에 지난번 뒤 새로 공시된 거래
    congress_cache: dict[str, dict | None] = {}
    congress_messages = 0
    for p in profiles:
        ids = (p["base_settings"] or {}).get("followMembers") or []
        if not ids:
            continue
        lines, newest, first_id = [], "", None
        prev = state.get((p["user_id"], CONGRESS_KEY))
        for mid in ids:
            if mid not in congress_cache:
                path = CONGRESS / f"{mid}.json"
                congress_cache[mid] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            m = congress_cache[mid]
            if not m or not m["trades"]:
                continue
            newest = max(newest, max(t[5] for t in m["trades"]))
            if prev:
                got = congress_lines(m["name"], m["trades"], prev["signal_date"])
                if got and first_id is None:
                    first_id = mid
                lines += got
        if not newest or (prev and prev["signal_date"] >= newest):
            continue
        if prev is None:
            first_seen += 1  # 처음: 기준만 기록
        updates.append({"user_id": p["user_id"], "symbol": CONGRESS_KEY, "zone": "neutral", "score": None, "signal_date": newest})
        if lines and p.get("push_token"):
            # 한 의원만이면 그 의원 화면으로, 여러 명이면 첫 의원 화면으로
            messages.append({"to": p["push_token"], "title": "지금이닷 · 의원 거래", "body": " · ".join(lines),
                             "sound": "default", "channelId": "signals", "data": {"screen": "CongressMember", "id": first_id}})
            congress_messages += 1

    print(f"{today} 알림 대상 {len(profiles)}명 · 종목 상태 {len(updates)}건(처음 {first_seen}) · 보낼 알림 {len(messages)}건"
          f"(내부자 {insider_messages}, 시장 {market_messages}, 의원 {congress_messages})")
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
