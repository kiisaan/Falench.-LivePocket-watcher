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


def is_falench_in_cast(soup):
    """
    出演者情報セクション（または「出演」「CAST」見出しの直下）に限定して
    Falench が記載されているか厳密判定する関数
    """
    # 1. ノイズとなる領域（関連イベント・注意事項・フッター・ヘッダー・モーダル）をあらかじめ削除
    for unwanted in soup.select(
        ".recommend, .other-events, .related-events, footer, .sidebar, #header, "
        ".modal, .notice, .attention, .faq, .ticket-info, #ticket-info"
    ):
        unwanted.decompose()

    # 2. LivePocketの指定クラス（出演者欄）をピンポイント取得
    cast_elements = soup.select(".cast, .performer, .artist, .event-cast, #cast, .cast-list")
    for elem in cast_elements:
        if "falench" in elem.get_text().lower():
            print(f"[CHECK] クラス判定でFalenchを発見: {elem.get_text()[:30]}")
            return True

    # 3. 「出演」「CAST」「ARTIST」などの見出し要素を探し、その配下・直後のテキストのみを検証
    keywords = ["出演", "cast", "artist", "出演者", "パーソナリティ"]
    
    for h_tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "dt", "th", "strong", "p", "div"]):
        tag_text = h_tag.get_text().strip().lower()
        
        # 見出し自体が「出演」や「CAST」などのキーワードを含んでいるか判定
        if any(kw == tag_text or kw in tag_text for kw in keywords):
            # ① 見出しタグの親要素の中身をチェック
            parent = h_tag.parent
            if parent and "falench" in parent.get_text().lower():
                print(f"[CHECK] 見出し親要素でFalenchを発見")
                return True

            # ② 見出しタグの「次のお兄さん・弟要素（Sibling）」をチェック
            curr = h_tag.next_sibling
            search_count = 0
            while curr and search_count < 3:
                if hasattr(curr, "get_text"):
                    if "falench" in curr.get_text().lower():
                        print(f"[CHECK] 見出し隣接要素でFalenchを発見")
                        return True
                curr = curr.next_sibling
                search_count += 1

    # どこにも出演情報として記載されていない場合は False
    return False


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

        # 2. 各イベントの詳細ページを開き「出演欄」にFalenchがあるか正確に検証
        for url in candidate_urls:
            try:
                print(f"[DEBUG] イベント詳細を検証中: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1000)

                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                # 出演者欄の限定チェック
                if is_falench_in_cast(detail_soup):
                    title_tag = detail_soup.find("h1") or detail_soup.find("title")
                    raw_title = title_tag.get_text(strip=True) if title_tag else "Falench. 出演ライブ"

                    clean_title = raw_title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    if len(clean_title) > 70:
                        clean_title = clean_title[:70] + "..."

                    print(f"[MATCH] ★出演者欄にFalench.の出演を確認！: {clean_title} ({url})")
                    new_events.append({"title": clean_title, "url": url})
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
        message_text += f"📌 {event['title']}\n🔗 {event['url']}\n\n"

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
