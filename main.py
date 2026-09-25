import os
import requests
from bs4 import BeautifulSoup

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

# 検索ワード指定のURLに変更
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

def fetch_events(notified_urls):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
    }
    
    print(f"[DEBUG] 取得元URL: {TARGET_URL}")
    response = requests.get(TARGET_URL, headers=headers)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    new_events = []
    
    # ページ内のすべての <a> タグから /event/ または /e/ を含むリンクを収集
    links = soup.find_all("a", href=True)
    print(f"[DEBUG] ページ内で発見した全リンク数: {len(links)}")
    
    seen_urls = set()
    for a_tag in links:
        href = a_tag["href"]
        
        # LivePocketのイベント詳細ページURLパターン (/event/detail/ や /e/ xxx)
        if "/e/" not in href and "/event/detail/" not in href:
            continue
            
        full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
        clean_url = full_url.split("?")[0]
        
        # 検索一覧ページ自体のURLなどを除外
        if "/event/search" in clean_url:
            continue

        if clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)
        
        if clean_url in notified_urls:
            print(f"[SKIP] 通知済みのためスキップ: {clean_url}")
            continue
        
        # タイトル文字列の整形
        title = a_tag.get_text(strip=True) or "Falench. 掲載イベント"
        if len(title) > 60:
            title = title[:60] + "..."
            
        new_events.append({"title": title, "url": clean_url})
        
    print(f"[DEBUG] 抽出された新着イベント数: {len(new_events)}")
    return new_events

def send_line_notification(events):
    if not events:
        print("[INFO] 送信する新着イベントがありません。")
        return False

    message_text = "🎉 【Falench.】新しいチケット・イベントが見つかりました！\n\n"
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
    new_events = fetch_events(notified_urls)
    
    if new_events:
        success = send_line_notification(new_events)
        if success:
            new_urls = {e["url"] for e in new_events}
            save_notified_urls(new_urls, notified_urls)
