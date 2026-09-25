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
    for unwanted in soup.select(
        ".recommend, .other-events, .related-events, footer, header, #header, "
        ".sidebar, .other-event-list, .recommend-event, .seller-event, "
        "[class*='recommend'], [class*='other'], [id*='recommend'], [id*='other']"
    ):
        unwanted.decompose()

    pattern = re.compile(r'falench(?:\.|\b)', re.IGNORECASE)

    title_element = soup.find("h1") or soup.find("title")
    if title_element and pattern.search(title_element.get_text()):
        return True

    main_content = soup.select_one("#event-detail, .event-detail, .main-content, #main")
    target_soup = main_content if main_content else soup

    blocks = target_soup.find_all(["div", "p", "li", "td", "span", "dd", "dt"])

    for block in blocks:
        block_text = block.get_text(strip=True)
        if pattern.search(block_text):
            if len(block_text) < 300:
                print(f"[CHECK] 正確な「Falench」の一致を確認: {block_text[:50]}")
                return True

    return False


def extract_event_details(soup):
    """
    LivePocket特有のタグ構造に対応した開催日程・チケット販売期間抽出関数
    """
    event_date = None
    sales_period = None

    # 1. 開催日時の抽出（LivePocketの概要テーブル・詳細ブロックから収集）
    date_patterns = [
        # 定義リスト (dt / dd) のパターン
        ("dt", ["日程", "日時", "開催日", "開催日時"]),
        ("th", ["日程", "日時", "開催日", "開催日時"]),
        ("span", ["日程", "日時", "開催日", "開催日時"]),
        ("p", ["日程", "日時", "開催日", "開催日時"])
    ]

    for tag_name, keywords in date_patterns:
        if event_date:
            break
        for tag in soup.find_all(tag_name):
            txt = tag.get_text().strip()
            if any(kw == txt or kw in txt for kw in keywords):
                # 直後のdd, td, span等を取得
                sibling = tag.find_next_sibling(["dd", "td", "span", "p", "div"])
                if sibling:
                    val = sibling.get_text(separator=" ", strip=True)
                    if val and len(val) < 120:
                        event_date = val
                        break

    # 2. チケット販売期間の抽出（チケット一覧枠 .ticket_list, .ticket-list, .ticket_sales_period 等から直接取得）
    sales_patterns = [
        ("dt", ["販売期間", "受付期間", "申込期間", "販売"]),
        ("th", ["販売期間", "受付期間", "申込期間", "販売"]),
        ("span", ["販売期間", "受付期間", "申込期間", "販売"])
    ]

    for tag_name, keywords in sales_patterns:
        if sales_period:
            break
        for tag in soup.find_all(tag_name):
            txt = tag.get_text().strip()
            if any(kw == txt or kw in txt for kw in keywords):
                sibling = tag.find_next_sibling(["dd", "td", "span", "p", "div"])
                if sibling:
                    val = sibling.get_text(separator=" ", strip=True)
                    if val and len(val) < 150:
                        sales_period = val
                        break

    # 3. テキスト抽出で見つからない場合の正規表現バックアップ（日付フォーマットから逆引き）
    full_text = soup.get_text(separator="\n", strip=True)

    if not event_date:
        # 例: 2026/10/10(土) 18:00 または 2026年10月10日 などのパターンを探す
        date_match = re.search(r'(\d{4}[/年]\d{1,2}[/月]\d{1,2}日?\s*\(?[\u4e00-\u9fa5]?\)?\s*\d{1,2}:\d{2}~?)', full_text)
        if date_match:
            event_date = date_match.group(1)

    if not sales_period:
        # 例: 2026/09/20(日) 20:00 ～ 2026/10/09(金) 23:59 のパターンを探す
        period_match = re.search(r'(\d{4}[/年]\d{1,2}[/月]\d{1,2}.*?～.*?\d{4}[/年]\d{1,2}[/月]\d{1,2}.*?)(?=\n|$)', full_text)
        if period_match:
            sales_period = period_match.group(1)

    return event_date or "要確認（詳細ページ参照）", sales_period or "要確認（詳細ページ参照）"


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
                # JSレンダリング完了のため networkidle で待機
                page.goto(url, wait_until="networkidle", timeout=20000)
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

                    # 日程およびチケット販売受付期間の抽出
                    event_date, sales_period = extract_event_details(detail_soup)

                    print(f"[MATCH] ★Falench.の出演を確認！: {clean_title} ({url})")
                    print(f"       日程: {event_date} / 販売期間: {sales_period}")

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
