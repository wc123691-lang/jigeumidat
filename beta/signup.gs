// 베타 테스터 신청 받기 (Google Apps Script, 신청 명단 구글 시트에 붙여 넣는다).
// 페이지(docs/beta/index.html)가 구글 로그인 ID 토큰을 보내면, 토큰을 구글에 확인하고
// '신청자' 시트에 한 줄을 쓴다. 같은 이메일은 한 번만 쓴다.
// 배포: 배포 → 새 배포 → 웹 앱, 실행: 나, 액세스 권한: 모든 사용자.

const CLIENT_ID = '542908652380-9fgerpmh8nsahoo655aajb9qopskqpdv.apps.googleusercontent.com';
const SHEET = '신청자';

function doPost(e) {
  const out = (o) => ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
  try {
    const body = JSON.parse(e.postData.contents);
    const res = UrlFetchApp.fetch('https://oauth2.googleapis.com/tokeninfo?id_token=' + encodeURIComponent(body.token), { muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) return out({ ok: false, error: 'login' });
    const t = JSON.parse(res.getContentText());
    if (t.aud !== CLIENT_ID || t.email_verified !== 'true') return out({ ok: false, error: 'login' });

    // =, +, -, @ 로 시작하면 시트가 수식으로 읽으므로 앞에 ' 를 붙인다
    const text = (v) => { const s = String(v || '').trim().slice(0, 40); return /^[=+\-@]/.test(s) ? "'" + s : s; };
    const lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      const book = SpreadsheetApp.getActiveSpreadsheet();
      const sheet = book.getSheetByName(SHEET) || book.insertSheet(SHEET);
      if (sheet.getLastRow() === 0) sheet.appendRow(['신청 시각', '이름', '이메일', '휴대폰', '초대 보냄']);
      const emails = sheet.getLastRow() > 1 ? sheet.getRange(2, 3, sheet.getLastRow() - 1, 1).getValues().flat() : [];
      if (emails.includes(t.email)) return out({ ok: true, dup: true });
      sheet.appendRow([new Date(), text(body.name || t.name), t.email, text(body.phone), '']);
    } finally {
      lock.releaseLock();
    }
    return out({ ok: true });
  } catch (err) {
    return out({ ok: false, error: 'server' });
  }
}
