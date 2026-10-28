// 베타 테스터 신청 받기 (Google Apps Script, 신청 명단 구글 시트에 붙여 넣는다).
// 페이지(docs/beta/index.html)가 구글 로그인 ID 토큰을 보내면, 토큰을 구글에 확인하고
// '신청자' 시트에 한 줄을 쓴다. 같은 이메일은 한 번만 쓴다.
// 신청 뒤 자동 처리:
//   - 신청자에게: 테스트가 아직이면 접수 메일, 열렸으면(아래 두 링크가 있으면) 바로 참여 안내 메일
//   - 나에게: 새 신청 알림 (몇 번째인지)
//   - 테스트 연 날: sendInvitesNow 실행 → 기다리던 신청자 모두에게 바로 참여 안내
//   - 매일(daily): 혹시 못 보낸 참여 안내 다시, 안내 7일 뒤 중간 점검 메일
// 처음 한 번: setup 실행(매일 작업 등록, 권한 허용) → 배포 → 배포 관리 → 새 버전.
// 배포: 웹 앱, 실행: 나, 액세스 권한: 모든 사용자.

const CLIENT_ID = '542908652380-9fgerpmh8nsahoo655aajb9qopskqpdv.apps.googleusercontent.com';
const SHEET = '신청자';
const COLS = ['신청 시각', '이름', '이메일', '휴대폰', '초대 보냄', '중간 안내'];

// 테스트를 열면 채운다. 둘 다 있어야 참여 안내를 보낸다.
const GROUP_URL = 'https://groups.google.com/g/jigeumidat-beta'; // 테스터 구글 그룹 (Play Console 비공개 테스트의 테스터로 등록한 그룹)
const OPTIN_URL = 'https://play.google.com/apps/testing/com.jigeumidat.app'; // Play Console 비공개 테스트의 '테스터 참여 링크'
const PLAY_URL = 'https://play.google.com/store/apps/details?id=com.jigeumidat.app';
// Gmail 설정 '다른 주소에서 메일 보내기'에 이 주소가 있으면 이 주소로 보낸다. 없으면 내 계정으로 보내고 답장만 이 주소로.
const FROM = 'jigeumidat.app@gmail.com';

const open_ = () => Boolean(GROUP_URL && OPTIN_URL);

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
    const name = text(body.name || t.name);
    let sheet, row;
    const lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      sheet = sheet_();
      const emails = sheet.getLastRow() > 1 ? sheet.getRange(2, 3, sheet.getLastRow() - 1, 1).getValues().flat() : [];
      if (emails.includes(t.email)) return out({ ok: true, dup: true });
      sheet.appendRow([new Date(), name, t.email, text(body.phone), '', '']);
      row = sheet.getLastRow();
    } finally {
      lock.releaseLock();
    }

    // 메일이 실패해도 신청은 된 것으로 둔다 (못 보낸 안내는 daily가 다시 보낸다)
    try {
      if (open_()) invite_(sheet, row);
      else send_(t.email, '[지금이닷] 베타 테스터 신청이 접수됐어요', receiptText_(name));
    } catch (err) { console.error(err); }
    try {
      const me = Session.getEffectiveUser().getEmail();
      GmailApp.sendEmail(me, `[지금이닷] 새 베타 신청 ${row - 1}번째: ${name}`, `${name} (${t.email})\n휴대폰: ${body.phone || '-'}\n\n신청 명단: ${SpreadsheetApp.getActiveSpreadsheet().getUrl()}`);
    } catch (err) { console.error(err); }
    return out({ ok: true });
  } catch (err) {
    return out({ ok: false, error: 'server' });
  }
}

// 매일 한 번 (setup이 등록): 참여 안내 못 받은 신청자에게 안내, 안내 7일 지난 신청자에게 중간 점검
function daily() {
  const sheet = sheet_();
  if (sheet.getLastRow() < 2) return;
  const rows = sheet.getRange(2, 1, sheet.getLastRow() - 1, COLS.length).getValues();
  const week = 7 * 24 * 3600 * 1000;
  rows.forEach((r, i) => {
    const row = i + 2;
    const [, name, email, , invited, checked] = r;
    if (!email) return;
    try {
      if (!invited && open_()) invite_(sheet, row);
      else if (invited instanceof Date && !checked && Date.now() - invited.getTime() >= week) {
        send_(email, '[지금이닷] 테스트 1주일, 중간 점검이에요', checkText_(name));
        sheet.getRange(row, 6).setValue(new Date());
      }
    } catch (err) { console.error(err); }
  });
}

// 테스트를 연 날 직접 실행: 기다리던 신청자 모두에게 참여 안내를 지금 바로 보낸다
function sendInvitesNow() {
  if (!open_()) throw new Error('GROUP_URL과 OPTIN_URL을 먼저 채워 주세요.');
  daily();
}

// 처음 한 번 직접 실행: 매일 오전 9시 작업 등록 (이미 있으면 그대로)
function setup() {
  sheet_();
  if (!ScriptApp.getProjectTriggers().some((t) => t.getHandlerFunction() === 'daily')) {
    ScriptApp.newTrigger('daily').timeBased().everyDays(1).atHour(9).create();
  }
  console.log('준비 완료. 보내는 주소: ' + (GmailApp.getAliases().includes(FROM) ? FROM : Session.getEffectiveUser().getEmail() + ' (답장은 ' + FROM + ')'));
}

function sheet_() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = book.getSheetByName(SHEET) || book.insertSheet(SHEET);
  if (sheet.getLastRow() === 0) sheet.appendRow(COLS);
  else sheet.getRange(1, 1, 1, COLS.length).setValues([COLS]);
  return sheet;
}

function invite_(sheet, row) {
  const [, name, email] = sheet.getRange(row, 1, 1, 3).getValues()[0];
  send_(email, '[지금이닷] 베타 테스트 참여 방법 (3단계)', inviteText_(name));
  sheet.getRange(row, 5).setValue(new Date());
}

function send_(to, subject, body) {
  const opts = { name: '지금이닷', replyTo: FROM };
  if (GmailApp.getAliases().includes(FROM)) opts.from = FROM;
  GmailApp.sendEmail(to, subject, body, opts);
}

function receiptText_(name) {
  return `${name}님, 지금이닷 베타 테스터 신청 감사합니다!

지금은 Google Play 테스트를 준비하고 있어요.
테스트가 열리면 이 메일 주소로 참여 방법을 바로 보내드릴게요.

미리 기억해 주세요
· 앱 안의 광고는 누르지 말아 주세요. (광고 계정에 문제가 생길 수 있어요)
· 설치 후 2주 동안은 지우지 말고 가끔 열어 주세요. (정식 출시 조건이에요)

궁금한 점은 이 메일에 답장해 주세요.
지금이닷 드림`;
}

function inviteText_(name) {
  return `${name}님, 지금이닷 베타 테스트가 열렸어요!
아래 3단계를 순서대로, 신청하신 이 구글 계정으로 진행해 주세요.

1. 테스터 그룹 가입
   ${GROUP_URL}
   → 오른쪽 위 '그룹 참여' 버튼을 눌러 주세요.
     ('액세스할 권한이 없습니다'라는 글은 가입 전이라 보이는 거예요. 신경 쓰지 않으셔도 돼요.)

2. 테스트 참여
   ${OPTIN_URL}
   → '테스터 되기'를 눌러 주세요.

3. 앱 설치
   ${PLAY_URL}
   → Play 스토어에서 설치해 주세요. 2단계 직후엔 몇 분 뒤에 보일 수 있어요.

꼭 지켜 주세요
· 앱 안의 광고는 누르지 말아 주세요.
· 14일 동안 지우지 말고 가끔 열어 주세요. 중간에 빠지시면 정식 출시가 늦어져요.

쓰다가 불편한 점, 좋았던 점은 이 메일에 답장으로 편하게 보내 주세요.
지금이닷 드림`;
}

function checkText_(name) {
  return `${name}님, 지금이닷 테스트 1주일째예요. 함께해 주셔서 감사합니다!

정식 출시 조건이 '14일 동안 테스트 유지'라서, 남은 1주일도 앱을 지우지 말고 가끔 열어 주세요.

한 가지만 여쭤봐도 될까요?
· 써 보니 가장 쓸모 있었던 기능은?
· 헷갈리거나 불편했던 화면은?
이 메일에 한두 줄 답장만 주셔도 큰 도움이 돼요.

지금이닷 드림`;
}
