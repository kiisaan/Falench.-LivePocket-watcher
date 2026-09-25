import os
import requests
from bs4 import BeautifulSoup

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

TARGET_URL = "https://livepocket.jp/event/search?performer=Falench."
CACHE_FILE = "notified_urls.txt"

def load_notified_urls():
    """過去に通知済みのURLリストを読み込む"""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_notified_urls(new_urls, existing_urls):
    """新しいURLをファイルに追記・保存する"""
    all_urls = existing_urls.union(new_urls)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        for url in sorted(all_urls):
            f.write(f"{url}\n")

def fetch_events(notified_urls):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(TARGET_URL, headers=headers)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    new_events = []
    
    event_elements = soup.select(".event-list-item, .search-result-item, a[href*='/e/']")
    
    seen_urls = set()
    for elem in event_elements:
        href = elem.get("href") if elem.name == "a" else (elem.find("a")["href"] if elem.find("a") else None)
        if not href or "/e/" not in href:
            continue
            
        full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
        
        # 今回のループ内での重複チェック & 過去に通知済みかチェック
        if full_url in seen_urls or full_url in notified_urls:
            continue
        seen_urls.add(full_url)
        
        title = elem.get_text(strip=True) or "Falench. 掲載イベント"
        new_events.append({"title": title, "url": full_url})
        
    return new_events

def send_line_notification(events):
    if not events:
        print("新しいイベントはありませんでした。")
        return False

    message_text = "🎉 【Falench.】新しいチケット・イベントが追加されました！\n\n"
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
    if res.status_code == 200:
        print("新着イベントのLINE通知が完了しました。")
        return True
    else:
        print(f"LINE送信エラー: {res.status_code} - {res.text}")
        return False

if __name__ == "__main__":
    notified_urls = load_notified_urls()
    new_events = fetch_events(notified_urls)
    
    if new_events:
        success = send_line_notification(new_events)
        if success:
            new_urls = {e["url"] for e in new_events}
            save_notified_urls(new_urls, notified_urls)
