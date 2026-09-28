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


def extract_event_details_from_page(page):
    """
    LivePocketの実DOM構造に合わせて【開催日時】と【販売期間】を確実に抽出する超強力なJavaScript解析ロジック
    """
    # 画面下部までスクロールして動的読み込みを確実に発火
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(1000)
    except Exception:
        pass

    details = page.evaluate("""() => {
        let dateStr = "";
        let salesList = [];

        // ==========================================
        // 1. 開催日時の抽出ロジック
        // ==========================================

        // (1) dl / dt / dd または th / td 構造から探す
        const allLabels = document.querySelectorAll('dt, th, span, div, p, strong, td');
        for (let labelNode of allLabels) {
            const txt = (labelNode.innerText || "").trim();
            if (txt === "日時" || txt === "日程" || txt === "開催日時" || txt === "開催日" || txt.includes("公演日時")) {
                // 隣接する要素（dd や td など）を特定
                let valNode = labelNode.nextElementSibling;
                if (!valNode && labelNode.parentElement) {
                    valNode = labelNode.parentElement.querySelector('dd, td, div, span');
                }
                if (valNode) {
                    const valText = (valNode.innerText || "").trim();
                    if (valText && valText.length < 150) {
                        dateStr = valText;
                        break;
                    }
                }
            }
        }

        // (2) LivePocketの日程表示専用クラス／IDから取得
        if (!dateStr) {
            const dateSelectors = [
                '#event_date', '.event_date', '.event-date',
                '.event-detail-time', '.event-time', '.event_time',
                '.schedule-time', '.date-box', '.event-schedule'
            ];
            for (let sel of dateSelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const txt = (el.innerText || "").trim();
                    if (txt && txt.length < 150) {
                        dateStr = txt;
                        break;
                    }
                }
            }
        }

        // (3) 全体テキストから正規表現による強力なフォールバック
        if (!dateStr) {
            const bodyText = document.body.innerText;
            // 2026/10/15(木) 18:00 または 2026年10月15日(木) 開場17:30 のようなパターンを判定
            const match = bodyText.match(/(?:\\d{4}[\\/\\.-]\\d{1,2}[\\/\\.-]\\d{1,2}|\\d{4}年\\d{1,2}月\\d{1,2}日)\\s*(?:\\([^\\)]+\\))?\\s*(?:[0-2]?\\d:[0-5]\\d)?/);
            if (match) {
                dateStr = match[0];
            }
        }

        // ==========================================
        // 2. チケット販売期間の抽出ロジック
        // ==========================================

        // LivePocketのチケット枠ブロックをすべて取得
        const ticketBlocks = document.querySelectorAll('.ticket_item, .ticket-item, .ticket-info, .ticket_info, .ticket-detail, .ticket-box, [class*="ticket"]');

        ticketBlocks.forEach(box => {
            // チケット枠内の販売期間を示すクラスを探す
            const periodElems = box.querySelectorAll('.sale_period, .sales_period, .ticket_period, .period, .sale-period, .sales-period, .sales-date, .sale-date, [class*="period"]');
            periodElems.forEach(p => {
                const pText = (p.innerText || "").trim();
                if (pText && (pText.includes("～") || pText.includes("~") || pText.includes("販売") || pText.includes("受付")) && pText.match(/\\d{1,2}[\\/\\.-]\\d{1,2}|\\d{1,2}月\\d{1,2}日/)) {
                    if (!salesList.includes(pText) && pText.length < 150) {
                        salesList.push(pText);
                    }
                }
            });
        });

        // テキスト行全体からのフォールバック走査
        if (salesList.length === 0) {
            const bodyLines = document.body.innerText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            for (let line of bodyLines) {
                if ((line.includes("販売期間") || line.includes("受付期間") || line.includes("申込期間") || (line.includes("販売") && line.includes("～"))) && line.match(/\\d{1,2}[\\/\\.-]\\d{1,2}|\\d{1,2}月\\d{1,2}日/)) {
                    if (!salesList.includes(line) && line.length < 150) {
                        salesList.push(line);
                    }
                }
            }
        }

        return {
            event_date: dateStr.trim(),
            sales_period: salesList.length > 0 ? salesList.join(" / ") : ""
        };
    }""")

    event_date = details.get("event_date") or "要確認（詳細ページ参照）"
    sales_period = details.get("sales_period") or "要確認（詳細ページ参照）"

    # 不要な連続改行や多重スペースのクリーンアップ
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
                page.wait_for_timeout(2000)  # JSレンダリング待機

                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                if is_falench_performing(detail_soup):
                    title_tag = detail_soup.find("h1") or detail_soup.find("title")
                    raw_title = title_tag.get_text(strip=True) if title_tag else "Falench. 出演ライブ"

                    clean_title = raw_title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    if len(clean_title) > 70:
                        clean_title = clean_title[:70] + "..."

                    # イベント詳細（開催日時・販売期間）を精密抽出
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
