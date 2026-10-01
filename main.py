import os
import re
import json
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

CACHE_FILE = "notified_urls.txt"
ORGANIZERS_FILE = "organizers.json"
DEBUG_DIR = "debug_pages"  # 抽出に失敗したページのテキスト/HTMLを保存する場所

UNKNOWN = "要確認（詳細ページ参照）"


def load_organizers():
    if os.path.exists(ORGANIZERS_FILE):
        try:
            with open(ORGANIZERS_FILE, "r", encoding="utf-8") as f:
                organizers = json.load(f)
                print(f"[DEBUG] {len(organizers)} 件の主催者を設定ファイルから読み込みました。")
                return organizers
        except Exception as e:
            print(f"[WARN] 設定ファイルの読み込みエラー ({ORGANIZERS_FILE}): {e}")
    else:
        print(f"[WARN] {ORGANIZERS_FILE} が見つかりません。デフォルトの検索のみ実行します。")
    return []


def load_notified_urls():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            urls = set(line.strip() for line in f if line.strip())
            print(f"[DEBUG] キャッシュから読み込んだ通知済みURL数: {len(urls)}")
            return urls
    print("[DEBUG] キャッシュファイルが存在しません（初回実行）。")
    return set()


def save_notified_urls(new_urls, existing_urls):
    all_urls = existing_urls.union(new_urls)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        for url in sorted(all_urls):
            f.write(f"{url}\n")
    print(f"[DEBUG] キャッシュに合計 {len(all_urls)} 件保存しました。")


def is_falench_performing(soup):
    """
    イベント詳細ページのメイン本文エリア内に Falench が出演者として記載されているか高精度に判定
    """
    soup_copy = BeautifulSoup(str(soup), "html.parser")
    for unwanted in soup_copy.select(
        ".recommend, .other-events, .related-events, footer, header, #header, "
        ".sidebar, .other-event-list, .recommend-event, .seller-event, "
        "[class*='recommend'], [class*='other'], [id*='recommend'], [id*='other']"
    ):
        unwanted.decompose()

    pattern = re.compile(r'falench(?:\.|\b)', re.IGNORECASE)

    title_element = soup_copy.find("h1") or soup_copy.find("title")
    if title_element and pattern.search(title_element.get_text()):
        return True

    main_content = soup_copy.select_one("#event-detail, .event-detail, .main-content, #main")
    target_soup = main_content if main_content else soup_copy

    blocks = target_soup.find_all(["div", "p", "li", "td", "span", "dd", "dt"])

    for block in blocks:
        block_text = block.get_text(strip=True)
        if pattern.search(block_text):
            if len(block_text) < 300:
                print(f"[CHECK] 正確な「Falench」の一致を確認: {block_text[:50]}")
                return True

    return False


# =====================================================================
# 開催日時・販売期間の抽出
#
# 方針: DOM構造（dt/ddの隣接関係など）に依存せず、ページ全体を「行テキスト」に
#       変換してから、ラベル行 + 日付パターンで取り出す。
#   1) page.inner_text("body")        … 画面に見えているテキスト
#   2) BeautifulSoup.get_text("\n")   … 非表示(折りたたみ等)の要素も含むテキスト
#   3) JSON-LD (schema.org/Event)     … 埋め込まれていれば最も正確
# の順に試し、どれも失敗した場合は debug_pages/ にダンプを保存する。
# =====================================================================

# 日付・時刻パターン
DATE_STRICT = r'20\d{2}\s*(?:[/.\-]|年)\s*\d{1,2}\s*(?:[/.\-]|月)\s*\d{1,2}\s*日?'
DATE_LOOSE = r'(?:20\d{2}\s*(?:[/.\-]|年)\s*)?\d{1,2}\s*(?:[/.\-]|月)\s*\d{1,2}\s*日?'
WEEKDAY = r'(?:\s*[\(（][^\)）]{1,4}[\)）])?'
TIME = r'\d{1,2}\s*[:：]\s*\d{2}'
DT_STRICT = rf'{DATE_STRICT}{WEEKDAY}(?:\s*{TIME})?'
DT_LOOSE = rf'{DATE_LOOSE}{WEEKDAY}(?:\s*{TIME})?'
RANGE_SEP = r'(?:[～〜~–—]|\s-\s)'
RANGE_ANY = re.compile(rf'{DT_STRICT}\s*{RANGE_SEP}\s*(?:{DT_LOOSE}|{TIME})?')
RANGE_FULL = re.compile(rf'{DT_STRICT}\s*{RANGE_SEP}\s*{DT_LOOSE}')

# ラベル候補
DATE_LABELS = ["開催日時", "公演日時", "開催日", "公演日", "日時", "日程", "開催期間", "date"]
SALES_LABELS = [
    "チケット販売期間", "販売期間", "受付期間", "申込期間", "申し込み期間",
    "発売期間", "販売日程", "受付日程", "一般発売", "販売開始", "発売日",
    "sales period", "reception period", "application period",
]
SALES_KW = re.compile(r'販売|発売|受付|申込|申し込み|締切|締め切り|まで|から')
NOT_EVENT_DATE_KW = re.compile(r'販売|発売|受付|申込|申し込み|締切|締め切り|期限|更新|投稿|公開|まで|から|入金|支払')

# ラベルの次行以降に続く「値の続き」とみなす行
CONT_RE = re.compile(
    r'^(?:[\(（]|\d{1,2}\s*[:：]\s*\d{2}|20\d{2}\s*[/.\-年]|開場|開演|OPEN|START|Open|Start|open|start|[～〜~–—])'
)


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_WD = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}


def _normalize_en(line):
    """
    WOVN等で英語化された表記を日本語ページ相当に直す。
      "2026 year Oct. 18 day (Sun)" -> "2026/10/18(日)"
      "2026 Oct. 13 (Tue)"          -> "2026/10/13(火)"
    """
    def repl(m):
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if not mon:
            return m.group(0)
        return f"{m.group(1)}/{mon:02d}/{int(m.group(3)):02d}"

    line = re.sub(
        r'(20\d{2})\s*(?:year(?:\(s\)|s)?)?\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*(?:day(?:\(s\)|s)?)?',
        repl, line)
    line = re.sub(r'\((Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\)',
                  lambda m: f"({_WD[m.group(1).lower()]})", line)
    return line


def _clean_lines(text):
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r'[ \t\u3000\xa0]+', ' ', raw).strip()
        if line:
            lines.append(_normalize_en(line))
    return lines


def _soup_text(html):
    """非表示要素も含めたテキスト（不要領域は除去）"""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "header", "footer"]):
        t.decompose()
    for t in soup.select(".recommend, .other-events, .related-events, .recommend-event, .other-event-list"):
        t.decompose()
    return soup.get_text("\n", strip=True)


def _labeled_values(lines, labels, strict=False):
    """
    「ラベル 値」または「ラベル\\n値」形式から、日付を含む値をすべて取り出す（ジェネレータ）。
    値が複数行に分かれている場合（日付 / (木) / 18:00 など）は連結する。
    """
    date_pat = DT_STRICT if strict else DT_LOOSE
    for i, line in enumerate(lines):
        for label in labels:
            if not line.lower().startswith(label.lower()):
                continue
            rest = line[len(label):].lstrip(" :：")
            cands = [(rest, i)] if rest else []
            cands += [(lines[j], j) for j in range(i + 1, min(i + 4, len(lines)))]
            matched = False
            for text, j in cands:
                if len(text) < 150 and re.search(date_pat, text):
                    parts = [text]
                    for k in range(j + 1, min(j + 4, len(lines))):
                        if len(lines[k]) < 40 and CONT_RE.match(lines[k]):
                            parts.append(lines[k])
                        else:
                            break
                    yield re.sub(r'\s+', ' ', " ".join(parts)).strip()
                    matched = True
                    break
            if matched:
                break


def _labeled_value(lines, labels, strict=False):
    return next(_labeled_values(lines, labels, strict), "")


def _guess_event_date(lines):
    """ラベルが見つからない場合のヒューリスティック"""
    for line in lines:
        if (len(line) < 150 and re.search(DT_STRICT, line)
                and re.search(r'開場|開演|OPEN|START', line, re.IGNORECASE)
                and not NOT_EVENT_DATE_KW.search(line)):
            return re.sub(r'\s+', ' ', line).strip()
    for line in lines:
        if len(line) < 80 and re.search(DT_STRICT, line) and not NOT_EVENT_DATE_KW.search(line):
            return re.sub(r'\s+', ' ', line).strip()
    return ""


SECTION_CUT_RE = re.compile(r'同じ(?:主催|販売元)|販売元の他|主催者の他|Events from the same', re.IGNORECASE)


def _extract_sales(lines, event_date=""):
    found = []

    def add(s):
        s = re.sub(r'\s+', ' ', s).strip()
        if not s or len(s) >= 150:
            return
        if event_date and s == re.sub(r'\s+', ' ', event_date).strip():
            return
        if any(s in f or f in s for f in found):
            return
        found.append(s)

    # 1) ラベル付き（販売期間: ... など）を最優先。チケット種別ごとに複数あれば全部拾う
    for v in _labeled_values(lines, SALES_LABELS):
        add(v)
    if found:
        return " / ".join(found[:4])

    # 2) ラベルが無い場合のみのフォールバック。
    #    「同じ主催者のイベント」以降は他イベントなので対象外にする
    for idx, line in enumerate(lines):
        if SECTION_CUT_RE.search(line):
            lines = lines[:idx]
            break

    for line in lines:
        if any(line.lower().startswith(l.lower()) for l in DATE_LABELS):
            continue
        # 同日の範囲（10/13 ～ 10/13）は開催日程であって販売期間ではない
        if len(line) < 150 and RANGE_ANY.search(line) and _collapse_same_day(line) == line:
            add(line)

    flat = " ".join(lines)
    for m in RANGE_FULL.finditer(flat):
        if _collapse_same_day(m.group(0)) == m.group(0):
            add(m.group(0))

    if not found:
        for line in lines:
            if any(line.lower().startswith(l.lower()) for l in DATE_LABELS):
                continue
            if len(line) < 100 and SALES_KW.search(line) and re.search(DT_LOOSE, line):
                add(line)

    return " / ".join(found[:4])


def _collapse_same_day(s):
    """「10/18(日) ～ 10/18(日)」のような同日範囲を1日にまとめる"""
    parts = re.split(r'\s*[～〜~–—]\s*', s)
    if len(parts) == 2:
        d1, d2 = re.search(DATE_STRICT, parts[0]), re.search(DATE_STRICT, parts[1])
        if d1 and d2 and re.sub(r'\s+', '', d1.group(0)) == re.sub(r'\s+', '', d2.group(0)):
            return parts[0].strip()
    return s


def _find_open_start(lines):
    """OPEN19:25/START19:40 形式の開場・開演時刻を探す"""
    pat = re.compile(
        r'(?:OPEN|開場)\s*\d{1,2}[:：]\d{2}(?:\s*[/／]\s*(?:START|開演)\s*\d{1,2}[:：]\d{2})?',
        re.IGNORECASE)
    for line in lines:
        if len(line) < 80:
            m = pat.search(line)
            if m:
                return m.group(0)
    return ""


def _iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _fmt_iso(s):
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?', str(s or ""))
    if not m:
        return ""
    y, mo, d, hh, mm = m.groups()
    return f"{y}/{mo}/{d}" + (f" {hh}:{mm}" if hh and (hh, mm) != ("00", "00") else "")


def _from_jsonld(html):
    result = {"date": "", "sales": ""}
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or s.get_text())
        except Exception:
            continue
        for node in _iter_dicts(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if not any(isinstance(x, str) and x.endswith("Event") for x in types):
                continue
            start = _fmt_iso(node.get("startDate"))
            end = _fmt_iso(node.get("endDate"))
            if start and not result["date"]:
                result["date"] = start if (not end or end == start) else f"{start} ～ {end}"
            offers = node.get("offers")
            offers = offers if isinstance(offers, list) else ([offers] if offers else [])
            periods = []
            for o in offers:
                if not isinstance(o, dict):
                    continue
                vf, vt = _fmt_iso(o.get("validFrom")), _fmt_iso(o.get("validThrough"))
                if vf or vt:
                    p = f"{vf} ～ {vt}".strip()
                    if p not in periods:
                        periods.append(p)
            if periods and not result["sales"]:
                result["sales"] = " / ".join(periods[:4])
    return result


def _dump_debug(url, body_text, html):
    lines = _clean_lines(body_text)
    html = html or ""
    print(f"----- [DUMP] {url} -----")
    print(f"  body行数={len(lines)} / html長={len(html)} / iframe数={html.count('<iframe')}")
    for l in lines[:150]:
        print(f"  | {l[:120]}")
    print("----- [DUMP END] -----")

    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        slug = re.sub(r'[^A-Za-z0-9_-]+', "_", url.rstrip("/").split("/")[-1])[:60] or "page"
        with open(os.path.join(DEBUG_DIR, f"{slug}.txt"), "w", encoding="utf-8") as f:
            f.write(body_text or "")
        with open(os.path.join(DEBUG_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"[WARN] デバッグ保存失敗: {e}")


def extract_event_details_from_page(page, url=""):
    """
    LivePocketのイベント詳細ページから【開催日時】と【販売期間】を抽出する。
    """
    # 遅延描画される要素を読み込ませる
    try:
        for ratio in (1 / 3, 2 / 3, 1):
            page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio});")
            page.wait_for_timeout(600)
        page.wait_for_timeout(800)
    except Exception:
        pass

    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""
    html = page.content()

    texts = [_clean_lines(body_text), _clean_lines(_soup_text(html))]
    ld = _from_jsonld(html)

    # --- 開催日時 ---
    event_date = ""
    event_lines = texts[0]
    for lines in texts:
        event_date = _labeled_value(lines, DATE_LABELS)
        if event_date:
            event_lines = lines
            break
    if not event_date:
        event_date = ld["date"]
    if not event_date:
        for lines in texts:
            event_date = _guess_event_date(lines)
            if event_date:
                event_lines = lines
                break

    event_date_raw = event_date  # 販売期間の誤検出除外用に、整形前の値を保持
    if event_date:
        event_date = _collapse_same_day(event_date)
        if not re.search(TIME, event_date):
            open_start = _find_open_start(event_lines) or _find_open_start(texts[1])
            if open_start:
                event_date = f"{event_date} {open_start}"

    # --- 販売期間 ---
    sales_period = ""
    for lines in texts:
        sales_period = _extract_sales(lines, event_date_raw)
        if sales_period:
            break
    if not sales_period:
        sales_period = ld["sales"]

    if not event_date or not sales_period:
        print(f"[DEBUG] 抽出不足 date={bool(event_date)} sales={bool(sales_period)} → ダンプ保存")
        _dump_debug(url, body_text, html)

    return (event_date or UNKNOWN), (sales_period or UNKNOWN)


def fetch_falench_events(notified_urls):
    new_events = []
    candidate_urls = set()

    organizers = load_organizers()

    search_urls = [
        "https://livepocket.jp/event/search?search_word=Falench",
    ] + [org["url"] for org in organizers if "url" in org]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"},
        )
        page = context.new_page()

        # 1. 各ソースからイベント詳細URLを収集
        for target_url in search_urls:
            print(f"[DEBUG] ページをスキャン中: {target_url}")
            try:
                page.goto(target_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(2000)

                html_content = page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                links = soup.find_all("a", href=True)
                for a_tag in links:
                    href = a_tag["href"]
                    if "/e/" not in href and "/event/detail/" not in href:
                        continue

                    full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
                    clean_url = full_url.split("?")[0]

                    if "/event/search" in clean_url:
                        continue

                    if clean_url in notified_urls:
                        continue

                    candidate_urls.add(clean_url)
            except Exception as e:
                print(f"[WARN] スキャン失敗 ({target_url}): {e}")

        print(f"[DEBUG] 収集された検証対象のイベント総数: {len(candidate_urls)}")

        # 2. 各イベントの詳細ページを開き検証・情報抽出
        for url in candidate_urls:
            try:
                print(f"[DEBUG] イベント詳細を検証中: {url}")
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)

                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                if is_falench_performing(detail_soup):
                    title_tag = detail_soup.find("h1") or detail_soup.find("title")
                    raw_title = title_tag.get_text(strip=True) if title_tag else "Falench. 出演ライブ"

                    clean_title = raw_title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    if len(clean_title) > 70:
                        clean_title = clean_title[:70] + "..."

                    # イベント詳細（開催日時・販売期間）を抽出
                    event_date, sales_period = extract_event_details_from_page(page, url)

                    print(f"[MATCH] ★Falench.の出演を確認！: {clean_title} ({url})")
                    print(f"       📅 日程: {event_date}")
                    print(f"       🎟 販売期間: {sales_period}")

                    new_events.append({
                        "title": clean_title,
                        "url": url,
                        "date": event_date,
                        "sales_period": sales_period
                    })
                else:
                    print(f"[EXCLUDE] Falench非出演のため除外: {url}")

            except Exception as e:
                print(f"[WARN] 詳細検証失敗 ({url}): {e}")

        browser.close()

    print(f"[DEBUG] 最終抽出された「Falench.」出演ライブ数: {len(new_events)}")
    return new_events


def send_line_notification(events):
    if not events:
        print("[INFO] 送信する新着イベントがありません。")
        return False

    message_text = "🎉 【Falench.】出演のチケット・ライブ情報が見つかりました！\n\n"
    for event in events:
        message_text += (
            f"📌 {event['title']}\n"
            f"📅 日程: {event['date']}\n"
            f"🎟 販売期間: {event['sales_period']}\n"
            f"🔗 {event['url']}\n\n"
        )

    endpoint = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [
            {
                "type": "text",
                "text": message_text.strip()
            }
        ]
    }

    res = requests.post(endpoint, json=payload, headers=headers, timeout=30)
    print(f"[DEBUG] LINE APIレスポンスコード: {res.status_code}")
    print(f"[DEBUG] LINE APIレスポンス詳細: {res.text}")

    if res.status_code == 200:
        print("[SUCCESS] LINEへの通知が成功しました！")
        return True
    else:
        print(f"[ERROR] LINE送信失敗: {res.status_code}")
        return False


if __name__ == "__main__":
    notified_urls = load_notified_urls()
    new_events = fetch_falench_events(notified_urls)

    if new_events:
        success = send_line_notification(new_events)
        if success:
            new_urls = {e["url"] for e in new_events}
            save_notified_urls(new_urls, notified_urls)
