import os
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

TARGET_URL = "https://livepocket.jp/event/search?search_word=Falench."
CACHE_FILE = "notified_urls.txt"

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

def is_falench_in_detail_page(page, url):
    """個別イベントページを開き、ページ全体または出演者欄にFalenchが含まれるか確認する"""
    try:
        print(f"[DEBUG] 詳細ページを検証中: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1000)
        
        detail_html = page.content()
        soup = BeautifulSoup(detail_html, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True)
        
        # 大小文字・ドットの有無を無視して「falench」が含まれるか判定
        if "falench" in page_text.lower():
            # H1タグ等から正式なライブタイトルを取得
            title_tag = soup.find("h1") or soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "Falench. 出演ライブ"
            # 余計なサイト名などを削除
            title = title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
            return True, title
    except Exception as e:
        print(f"[WARN] 詳細ページの読み込みに失敗 ({url}): {e}")
        
    return False, ""

def fetch_events_with_playwright(notified_urls):
    print(f"[DEBUG] Playwrightで検索ページを開きます: {TARGET_URL}")
    
    candidate_urls = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800}
        )
        page = context.new_page()
        
        # 1. まず検索結果一覧ページを開く
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        
        search_html = page.content()
        soup = BeautifulSoup(search_html, "html.parser")
        
        # ページ内のイベントURL（/e/ または /event/detail/）をすべて候補として収集
        links = soup.find_all("a", href=True)
        seen_urls = set()
        
        for a_tag in links:
            href = a_tag["href"]
            if "/e/" not in href and "/event/detail/" not in href:
                continue
                
            full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
            clean_url = full_url.split("?")[0]
            
            if "/event/search" in clean_url or clean_url in seen_urls:
                continue
                
            seen_urls.add(clean_url)
            
            if clean_url in notified_urls:
                print(f"[SKIP] 通知済みのためスキップ: {clean_url}")
                continue
                
            candidate_urls.append(clean_url)

        print(f"[DEBUG] 検証対象の未通知イベント候補数: {len(candidate_urls)}")

        # 2. 候補URLを1つずつ開き、Falenchが出演者に含まれるか精査
        new_events = []
        for url in candidate_urls:
            is_target, title = is_falench_in_detail_page(page, url)
            if is_target:
                print(f"[MATCH] Falench.の出演を確認！: {title} ({url})")
                new_events.append({"title": title, "url": url})
            else:
                print(f"[EXCLUDE] Falenchが含まれないため除外: {url}")

        browser.close()

    print(f"[DEBUG] 抽出された「Falench.」出演ライブ数: {len(new_events)}")
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
    new_events = fetch_events_with_playwright(notified_urls)
    
    if new_events:
        success = send_line_notification(new_events)
        if success:
            new_urls = {e["url"] for e in new_events}
            save_notified_urls(new_urls, notified_urls)
