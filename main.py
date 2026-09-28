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
    LivePocketの実DOM構造に合わせて開催日程と販売期間を正確に切り出す関数
    """
    # ページ内コンテンツが完全にレンダリングされるまで明示的に待機・スクロール
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        page.wait_for_timeout(1000)
    except Exception:
        pass

    details = page.evaluate("""() => {
        let dateStr = "";
        let salesStr = "";

        // ===== 1. LivePocket特有のクラス/構造から開催日程を取得 =====
        // パターンA: .event-detail-time や .event-time クラス
        const timeElems = document.querySelectorAll('.event-detail-time, .event-time, .event_time, .schedule-time');
        for (let el of timeElems) {
            const txt = (el.innerText || "").trim();
            if (txt && txt.length < 150) {
                dateStr = txt.replace(/\\n+/g, ' ');
                break;
            }
        }

        // パターンB: <dl>構造（「日程」「日時」「開催日」のdtに対応するdd）
        if (!dateStr) {
            const dts = document.querySelectorAll('dl dt, table th, div dt');
            for (let dt of dts) {
                const label = (dt.innerText || "").trim();
                if (label.includes("日程") || label.includes("日時") || label.includes("開催")) {
                    const dd = dt.nextElementSibling;
                    if (dd) {
                        const val = (dd.innerText || "").trim();
                        if (val && val.length < 150) {
                            dateStr = val.replace(/\\n+/g, ' ');
                            break;
                        }
                    }
                }
            }
        }

        // パターンC: テキスト全体からの正規表現フォールバック（202x/xx/xx(金) xx:xx）
        if (!dateStr) {
            const bodyText = document.body.innerText;
            const dateMatch = bodyText.match(/(\\d{4}[\\/\\.-]\\d{1,2}[\\/\\.-]\\d{1,2}\\s*\\(?[^\\)\\n]*\\)?\\s*\\d{1,2}:\\d{2}(?:~|～)?)/);
            if (dateMatch) {
                dateStr = dateMatch[1];
            }
        }

        // ===== 2. LivePocket特有のチケット枠から販売期間を取得 =====
        const periods = [];

        // パターンA: LivePocketのチケット枠 (.ticket-detail, .ticket-sales-period, .sales-date 等)
        const ticketNodes = document.querySelectorAll('.ticket-detail, .ticket-sales-period, .sales-date, .sales-period, .period, [class*="sales"], [class*="ticket"]');
        ticketNodes.forEach(node => {
            const text = (node.innerText || "").trim();
            // 日付表記（月/日 または 年/月/日）と販売/受付/波線(～)が含まれる行を抽出
            if (text && (text.includes("～") || text.includes("~") || text.includes("販売") || text.includes("受付"))) {
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                for (let line of lines) {
                    if ((line.includes("～") || line.includes("~") || line.includes("販売") || line.includes("受付")) && line.match(/\\d{1,2}[\\/\\.-]\\d{1,2}/)) {
                        if (!periods.includes(line) && line.length < 150) {
                            periods.push(line);
                        }
                    }
                }
            }
        });

        // パターンB: ページ全体テキストからのフォールバック（販売期間：202x/xx/xx ～）
        if (periods.length === 0) {
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n').map(l => l.trim());
            for (let line of lines) {
                if ((line.includes("販売") || line.includes("受付")) && line.match(/\\d{1,2}[\\/\\.-]\\d{1,2}.*?(?:～|~|-).*?\\d{1,2}[\\/\\.-]\\d{1,2}/)) {
                    if (!periods.includes(line) && line.length < 150) {
                        periods.push(line);
                    }
                }
            }
        }

        if (periods.length > 0) {
            salesStr = periods.join(" / ");
        }

        return {
            event_date: dateStr.trim(),
            sales_period: salesStr.trim()
        };
    }""")

    event_date = details.get("event_date") or "要確認（詳細ページ参照）"
    sales_period = details.get("sales_period") or "要確認（詳細ページ参照）"

    # 不要な連続スペースの除去
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
                
                # LivePocketのコンテナ描画を待機
                try:
                    page.wait_for_selector(".event-detail-time, .event-detail, #event-detail, body", timeout=5000)
                except Exception:
                    pass

                page.wait_for_timeout(2000)  # JSレンダリング完了の最終待機

                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                if is_falench_performing(detail_soup):
                    title_tag = detail_soup.find("h1") or detail_soup.find("title")
                    raw_title = title_tag.get_text(strip=True) if title_tag else "Falench. 出演ライブ"

                    clean_title = raw_title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    if len(clean_title) > 70:
                        clean_title = clean_title[:70] + "..."

                    # LivePocket専用のDOM解析関数をコール
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
