/**
 * ふぁれんち管理参加表 自動更新用 Google Apps Script
 *
 * LivePocket / TicketDive の通知botから送られてきた新着ライブ情報を受け取り、
 * 「2026/11月」のような月タブの、公演日に対応する行の
 *   イベント名 / 会場 / チケ発時刻 / チケページ
 * を書き込む。参加欄・参加人数・スプシ列には触らない。
 *
 * 行の決め方（上から順に試す）
 *   1. チケページに同じURLが入っている行 → その行を更新
 *   2. 同じ日付で、チケページが空の行のうち
 *        a. イベント名が似ている行
 *        b. イベント名が空 or 「事務所主催」「対バン」「未定」などの仮の名前の行
 *      → その行を更新
 *   3. どれも無ければ、日付順の位置に行を追加
 *
 * セットアップは README の「スプレッドシート自動更新」を参照。
 */

const CONFIG = {
  TEMPLATE_SHEET: '202x/x月',     // 月タブが無いときにコピーする雛形
  RELEASED_TEXT: 'チケ発済み',
  TZ: 'Asia/Tokyo',
  // 同じ日付の「仮の行」とみなすイベント名（チケページが空のときだけ上書きする）
  PLACEHOLDER_RE: /^(?:|事務所?主催|対バン|未定|tba|tbd|[?？]+|-+|ー+)$/i,
  HEADERS: {
    date: '日付',
    name: 'イベント名',
    venue: '会場',
    release: 'チケ発時刻',
    page: 'チケページ',
  },
};

const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];

// ---------------------------------------------------------------------------
// Webアプリの入口
// ---------------------------------------------------------------------------
function doGet() {
  return json_({ ok: true, message: 'ふぁれんち参加表 自動更新は動作中です' });
}

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: 'JSONを読めません' });
  }

  const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
  if (!token || body.token !== token) {
    return json_({ ok: false, error: '合言葉(TOKEN)が一致しません' });
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    return json_({ ok: false, error: '他の更新処理が実行中です' });
  }
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const results = (body.events || []).map(function (ev) {
      try {
        return upsertEvent_(ss, ev);
      } catch (err) {
        return { ok: false, message: 'エラー: ' + err.message };
      }
    });
    SpreadsheetApp.flush();
    return json_({ ok: true, results: results });
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// 1件のイベントを書き込む
// ---------------------------------------------------------------------------
function upsertEvent_(ss, ev) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ev.date || '');
  if (!m) {
    return { ok: false, message: '公演日が読み取れなかったため未反映（' + (ev.title || '') + '）' };
  }
  const year = +m[1], month = +m[2], day = +m[3];
  const label = month + '/' + pad_(day) + '(' + WEEKDAYS[new Date(year, month - 1, day).getDay()] + ')';

  const sheet = getMonthSheet_(ss, year, month);
  if (!sheet) {
    return { ok: false, message: year + '/' + month + '月 のタブが無く、雛形「' + CONFIG.TEMPLATE_SHEET + '」も無いため未反映' };
  }

  const layout = readLayout_(sheet);
  if (!layout) {
    return { ok: false, message: sheet.getName() + ' に見出し行（日付・イベント名・チケページ）が見つかりません' };
  }

  const rows = readRows_(sheet, layout);
  const url = normUrl_(ev.url);
  const title = String(ev.title || '').trim();
  let target = null, how = '';

  // 1. 同じURLの行
  target = rows.find(function (r) { return url && r.url === url; });
  if (target) how = '更新';

  // 2. 同じ日付で、チケページが空の行
  if (!target) {
    const sameDay = rows.filter(function (r) { return r.month === month && r.day === day && !r.url; });
    target = sameDay.find(function (r) { return similar_(r.name, title); })
      || sameDay.find(function (r) { return CONFIG.PLACEHOLDER_RE.test(r.name.replace(/\s/g, '')); });
    if (target) how = '更新';
  }

  // 3. 行を追加
  if (!target) {
    const rowNo = insertRow_(sheet, layout, rows, month, day);
    target = { row: rowNo, name: '' };
    writeDate_(sheet, layout, rows, rowNo, year, month, day, label);
    how = '追加';
  }

  // 書き込み（空の値で既存の値を消さない）
  const r = target.row;
  if (title) sheet.getRange(r, layout.name).setValue(title);
  if (layout.venue && ev.venue) sheet.getRange(r, layout.venue).setValue(ev.venue);
  if (layout.release && ev.release_at) sheet.getRange(r, layout.release).setValue(releaseText_(ev.release_at));
  if (ev.url) sheet.getRange(r, layout.page).setValue(ev.url);

  const before = target.name && target.name !== title ? '（旧: ' + target.name + '）' : '';
  return {
    ok: true,
    row: r,
    message: sheet.getName() + ' ' + label + ' の行を' + how + before,
  };
}

// ---------------------------------------------------------------------------
// シート構造の読み取り
// ---------------------------------------------------------------------------
function getMonthSheet_(ss, year, month) {
  const want = year + '/' + month + '月';
  const found = ss.getSheets().find(function (s) {
    const mm = /^\s*(\d{4})\s*\/\s*0?(\d{1,2})\s*月\s*$/.exec(s.getName());
    return mm && +mm[1] === year && +mm[2] === month;
  });
  if (found) return found;

  const tpl = ss.getSheetByName(CONFIG.TEMPLATE_SHEET);
  if (!tpl) return null;
  const copy = tpl.copyTo(ss).setName(want);
  ss.setActiveSheet(copy);
  ss.moveActiveSheet(tpl.getIndex() + 1);  // 雛形のすぐ右（最新の月が左に来る並び）
  return copy;
}

function readLayout_(sheet) {
  const maxRow = Math.min(10, sheet.getLastRow());
  if (maxRow < 1) return null;
  const values = sheet.getRange(1, 1, maxRow, sheet.getLastColumn()).getDisplayValues();
  for (let i = 0; i < values.length; i++) {
    const row = values[i].map(function (v) { return String(v).trim(); });
    const col = function (h) { const k = row.indexOf(h); return k < 0 ? 0 : k + 1; };
    const layout = {
      header: i + 1,
      date: col(CONFIG.HEADERS.date),
      name: col(CONFIG.HEADERS.name),
      venue: col(CONFIG.HEADERS.venue),
      release: col(CONFIG.HEADERS.release),
      page: col(CONFIG.HEADERS.page),
    };
    if (layout.date && layout.name && layout.page) return layout;
  }
  return null;
}

function readRows_(sheet, layout) {
  const first = layout.header + 1;
  const last = sheet.getLastRow();
  if (last < first) return [];
  const n = last - first + 1;
  const width = sheet.getLastColumn();
  const values = sheet.getRange(first, 1, n, width).getValues();
  const disp = sheet.getRange(first, 1, n, width).getDisplayValues();
  const rows = [];
  for (let i = 0; i < n; i++) {
    const md = parseMonthDay_(values[i][layout.date - 1], disp[i][layout.date - 1]);
    rows.push({
      row: first + i,
      month: md ? md[0] : null,
      day: md ? md[1] : null,
      isDateObj: values[i][layout.date - 1] instanceof Date,
      name: clean_(disp[i][layout.name - 1]),
      url: normUrl_(disp[i][layout.page - 1]),
    });
  }
  return rows;
}

function parseMonthDay_(value, display) {
  if (value instanceof Date) return [value.getMonth() + 1, value.getDate()];
  const m = /(\d{1,2})\s*[\/月]\s*(\d{1,2})/.exec(clean_(display));
  return m ? [+m[1], +m[2]] : null;
}

// ---------------------------------------------------------------------------
// 行の追加
// ---------------------------------------------------------------------------
function insertRow_(sheet, layout, rows, month, day) {
  const key = month * 100 + day;
  let after = layout.header;          // この行の下に入れる
  let afterIdx = -1;
  rows.forEach(function (r, i) {
    if (r.month && r.month * 100 + r.day <= key) { after = r.row; afterIdx = i; }
  });

  // すぐ下が空行（日付もイベント名も空）なら、そこを使う（並び順は崩れない）
  const next = rows[afterIdx + 1];
  if (next && !next.month && !next.name && !next.url) return next.row;

  sheet.insertRowAfter(after);
  const newRow = after + 1;
  // 書式・入力規則（プルダウン）・数式を隣の行からコピー
  const srcRow = after > layout.header ? after : newRow + 1;
  if (srcRow <= sheet.getLastRow()) {
    const width = sheet.getLastColumn();
    const src = sheet.getRange(srcRow, 1, 1, width);
    const dst = sheet.getRange(newRow, 1, 1, width);
    src.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    src.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
    const formulas = src.getFormulasR1C1()[0];
    formulas.forEach(function (f, c) {
      if (f) sheet.getRange(newRow, c + 1).setFormulaR1C1(f);
    });
  }
  return newRow;
}

function writeDate_(sheet, layout, rows, rowNo, year, month, day, label) {
  const cell = sheet.getRange(rowNo, layout.date);
  const sample = rows.find(function (r) { return r.month; });
  if (sample && sample.isDateObj) {
    cell.setNumberFormat(sheet.getRange(sample.row, layout.date).getNumberFormat());
    cell.setValue(new Date(year, month - 1, day));
  } else {
    cell.setNumberFormat('@').setValue(label);
  }
}

// ---------------------------------------------------------------------------
// チケ発時刻
// ---------------------------------------------------------------------------
function nowKey_() {
  return Utilities.formatDate(new Date(), CONFIG.TZ, "yyyy-MM-dd'T'HH:mm");
}

/** "2026-11-13T21:00" → "11/13 21:00～"（既に過ぎていれば「チケ発済み」） */
function releaseText_(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  if (iso.slice(0, 16) <= nowKey_()) return CONFIG.RELEASED_TEXT;
  return (+m[2]) + '/' + (+m[3]) + ' ' + m[4] + ':' + m[5] + '～';
}

/**
 * 時間主導トリガー用：チケ発時刻を過ぎた「11/13 21:00～」を「チケ発済み」に書き換える。
 * setup() を実行すると15分おきに動くようになる。
 */
function markReleased() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const now = nowKey_();
  ss.getSheets().forEach(function (sheet) {
    const sm = /^\s*(\d{4})\s*\/\s*0?(\d{1,2})\s*月\s*$/.exec(sheet.getName());
    if (!sm) return;
    const layout = readLayout_(sheet);
    if (!layout || !layout.release) return;
    const first = layout.header + 1, last = sheet.getLastRow();
    if (last < first) return;
    const range = sheet.getRange(first, layout.release, last - first + 1, 1);
    const vals = range.getDisplayValues();
    let changed = false;
    const out = vals.map(function (v) {
      const m = /^\s*(\d{1,2})\/(\d{1,2})\s*(\d{1,2}):(\d{2})\s*[～〜~]?\s*$/.exec(v[0]);
      if (!m) return [v[0]];
      // 年は月タブの年。チケ発の月が公演月より大きければ前の年（12月発売→1月公演）
      const y = +m[1] > +sm[2] + 1 ? +sm[1] - 1 : +sm[1];
      const key = y + '-' + pad_(+m[1]) + '-' + pad_(+m[2]) + 'T' + pad_(+m[3]) + ':' + m[4];
      if (key <= now) { changed = true; return [CONFIG.RELEASED_TEXT]; }
      return [v[0]];
    });
    if (changed) range.setValues(out);
  });
}

// ---------------------------------------------------------------------------
// 初期設定（エディタから1回だけ実行）
// ---------------------------------------------------------------------------
function setup() {
  const props = PropertiesService.getScriptProperties();
  let token = props.getProperty('TOKEN');
  if (!token) {
    token = Utilities.getUuid().replace(/-/g, '');
    props.setProperty('TOKEN', token);
  }
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'markReleased') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('markReleased').timeBased().everyMinutes(15).create();
  Logger.log('SHEET_TOKEN に登録する合言葉: ' + token);
}

/** 動作確認用：実際に書き込むので、確認後に行を消してください */
function testUpsert() {
  const res = upsertEvent_(SpreadsheetApp.getActiveSpreadsheet(), {
    title: 'テスト公演（消してOK）',
    url: 'https://example.com/test',
    date: Utilities.formatDate(new Date(), CONFIG.TZ, 'yyyy-MM-dd'),
    venue: 'テスト会場',
    release_at: '2099-01-01T12:00',
  });
  Logger.log(JSON.stringify(res));
}

// ---------------------------------------------------------------------------
// 小物
// ---------------------------------------------------------------------------
function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function pad_(n) { return (n < 10 ? '0' : '') + n; }

function clean_(s) {
  return String(s == null ? '' : s).replace(/[​-‍﻿]/g, '').trim();
}

function normUrl_(u) {
  u = clean_(u);
  if (!/^https?:\/\//i.test(u)) return '';
  return u.replace(/^http:/i, 'https:').split('#')[0].split('?')[0].replace(/\/+$/, '').toLowerCase();
}

function normName_(s) {
  return clean_(s).normalize('NFKC').toLowerCase().replace(/[\s()（）「」『』【】\[\]!！?？・\-ー_.,、。~～:：]/g, '');
}

function similar_(a, b) {
  a = normName_(a); b = normName_(b);
  if (!a || !b) return false;
  if (a === b || a.indexOf(b) >= 0 || b.indexOf(a) >= 0) return true;
  // 先頭5文字が同じなら同じ公演とみなす（「すこフェス4MAN」と「すこフェス4MAN 第2部」など）
  return a.length >= 5 && b.length >= 5 && a.slice(0, 5) === b.slice(0, 5);
}
