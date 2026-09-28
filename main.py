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
    イベント詳細ページの「メイン本文エリア」内にのみ
    Falench が出演者として記載されているか高精度に判定する関数
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


def extract_event_details_from_page(page):
    """
    Playwrightで完全にレンダリングされたLivePocketのDOMから
    日程と販売期間を強固に取得する関数
    """
    # 画面下部までスクロールしてJSの遅延読み込みを完了させる
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
    except Exception:
        pass

    details = page.evaluate("""() => {
        let dateStr = "";
        let salesStr = "";

        // ----- 1. 日程の抽出 -----
        // 優先セレクター
        const dateSelectors = [
            '#event_date', '.event_date', '.event-date', '.date',
            '.schedule', '.event-time', '.event_time'
        ];
        for (let sel of dateSelectors) {
            const el = document.querySelector(sel);
            if (el && el.innerText.strip) {
                const text = el.innerText.trim();
                if (text && text.length < 120 && text.match(/\\d{1,2}[\\/\\.-]\\d{1,2}|\\d{4}/)) {
                    dateStr = text;
                    break;
                }
            }
        }

        // 定義リスト（dl/dt/dd）から「日時」「日程」「開催」を検索
        if (!dateStr) {
            const dls = document.querySelectorAll('dl, tr, div');
            for (let el of dls) {
                const text = el.innerText || "";
                if ((text.includes("日時") || text.includes("日程") || text.includes("開催日")) && text.match(/\\d{1,2}[\\/\\.-]\\d{1,2}|\\d{4}/)) {
                    const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                    for (let i = 0; i < lines.length; i++) {
                        if (lines[i].includes("日時") || lines[i].includes("日程") || lines[i].includes("開催日")) {
                            dateStr = lines.slice(i, i + 2).join(" ");
                            break;
                        }
                    }
                    if (dateStr) break;
                }
            }
        }

        // 全体テキストからのフォールバック（日付＋時間のパターン）
        if (!dateStr) {
            const bodyText = document.body.innerText;
            const match = bodyText.match(/(\\d{4}[\\/\\.-]\\d{1,2}[\\/\\.-]\\d{1,2}\\s*\\(?[^\\)\\n]*\\)?\\s*\\d{1,2}:\\d{2}~?)/);
            if (match) {
                dateStr = match[1];
            }
        }

        // ----- 2. 販売期間の抽出 -----
        const ticketPeriods = [];

        // LivePocketのチケット枠クラス（.period, .sale_period, .ticket_period, .sale-period等）
        const periodElements = document.querySelectorAll('.period, .sale_period, .sales_period, .ticket_period, .sale-period, .sales-period, .ticket-period, [class*="period"]');
        
        periodElements.forEach(el => {
            const text = (el.innerText || "").trim();
            if (text && (text.includes("～") || text.includes("~") || text.match(/\\d{1,2}[\\/\\.-]\\d{1,2}/))) {
                if (!ticketPeriods.includes(text) && text.length < 150) {
                    ticketPeriods.push(text);
                }
            }
        });

        // チケットリスト全体ブロックからのテキスト走査
        if (ticketPeriods.length === 0) {
            const ticketBoxes = document.querySelectorAll('.ticket-list, .ticket_info, .ticket-info, .ticket-item, #ticket_area, .ticket');
            ticketBoxes.forEach(box => {
                const lines = box.innerText.split('\\n').map(l => l.trim());
                for (let line of lines) {
                    if ((line.includes("販売") || line.includes("受付") || line.includes("～") || line.includes("~")) && line.match(/\\d{1,2}[\\/\\.-]\\d{1,2}/)) {
                        if (!ticketPeriods.includes(line) && line.length < 150) {
                            ticketPeriods.push(line);
                        }
                    }
                }
            });
        }

        // 全体テキストからの最終フォールバック
        if (ticketPeriods.length === 0) {
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n').map(l => l.trim());
            for (let line of lines) {
                if ((line.includes("販売期間") || line.includes("受付期間") || line.includes("申込期間")) && line.match(/\\d{1,2}[\\/\\.-]\\d{1,2}/)) {
                    ticketPeriods.push(line);
                    break;
                }
            }
        }

        if (ticketPeriods.length > 0) {
            salesStr = ticketPeriods.join(" / ");
        }

        return {
            event_date: dateStr.trim(),
            sales_period: salesStr.trim()
        };
    }""")

    event_date = details.get("event_date") or "要確認（詳細ページ参照）"
    sales_period = details.get("sales_period") or "要確認（詳細ページ参照）"

    # 改行や複数スペースの整形
    event_date = re.sub(r'\s+', ' ', event_date)
    sales_period = re.sub(r'\s+', ' ', sales_period)

    return event_date, sales_period


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
            viewport={'width': 1280, 'height': 800}
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
                page.wait_for_timeout(3000)  # JSレンダリング完了を確実にするため3秒待機

                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                if is_falench_performing(detail_soup):
                    title_tag = detail_soup.find("h1") or detail_soup.find("title")
                    raw_title = title_tag.get_text(strip=True) if title_tag else "Falench. 出演ライブ"

                    clean_title = raw_title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    if len(clean_title) > 70:
                        clean_title = clean_title[:70] + "..."

                    # DOM解析で日程・販売期間を確実に抽出
                    event_date, sales_period = extract_event_details_from_page(page)

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

    res = requests.post(endpoint, json=payload, headers=headers)
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
