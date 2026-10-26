"""상원 eFD(efdsearch.senate.gov) 접속 확인용. 한국에서는 지역 차단(403)이라 GitHub Actions(미국)에서 돌려 본다.
이용 동의(prohibition_agreement)는 사용자가 확인하고 허락했다. 공개 로그라 상태 코드와 건수만 찍는다.
"""

import json
import sys
import http.cookiejar
import re
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def req(url, data=None, ref=None):
    h = {"User-Agent": UA, "Accept": "text/html,application/json,*/*", "Accept-Language": "en-US,en;q=0.9"}
    if ref:
        h["Referer"] = ref
    r = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None, headers=h)
    try:
        with op.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


st, html = req("https://efdsearch.senate.gov/search/home/")
print("home", st, len(html))
tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
print("token", bool(tok))
if tok:
    st, html = req("https://efdsearch.senate.gov/search/home/", {"prohibition_agreement": "1", "csrfmiddlewaretoken": tok.group(1)},
                   "https://efdsearch.senate.gov/search/home/")
    print("agree", st, len(html))
    csrf = next((c.value for c in cj if c.name == "csrftoken"), "")
    st, body = req("https://efdsearch.senate.gov/search/report/data/", {
        "start": "0", "length": "5", "report_types": "[11]", "filer_types": "[]",
        "submitted_start_date": "01/01/2026 00:00:00", "submitted_end_date": "", "candidate_state": "", "senator_state": "",
        "office_id": "", "first_name": "", "last_name": "", "csrfmiddlewaretoken": csrf}, "https://efdsearch.senate.gov/search/")
    try:
        n = json.loads(body).get("recordsTotal")
    except Exception:
        n = None
    print("data", st, "reports", n)
    sys.exit(0 if n else 1)
sys.exit(1)
