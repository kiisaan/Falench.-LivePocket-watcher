import os
import requests
from bs4 import BeautifulSoup

# 環境変数からLINEのトークンと宛先IDを取得
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

TARGET_URL = "https://livepocket.jp/event/search?performer=Falench."

def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(TARGET_URL, headers=headers)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    events = []
    
    # LivePocketのイベントカード要素を抽出
    # ※サイト構造の変更に応じてセレクタを調整してください
    event_elements = soup.select(".event-list-item, .search-result-item, a[href*='/e/']")
    
    seen_urls = set()
    for elem in event_elements:
        href = elem.get("href") if elem.name == "a" else (elem.find("a")["href"] if elem.find("a") else None)
        if not href or "/e/" not in href:
            continue
            
        full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        
        title = elem.get_text(strip=True) or "Falench. 掲載イベント"
        events.append({"title": title, "url": full_url})
        
    return events

def send_line_notification(events):
    if not events:
        print("新規・該当イベントは見つかりませんでした。")
        return

    message_text = "🎵 【Falench.】ライブポケット新着・該当イベント\n\n"
    for event in events[:5]:  # 一度に送信する件数を制限（必要に応じて調整）
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
        print("LINEへの通知が完了しました。")
    else:
        print(f"LINE送信エラー: {res.status_code} - {res.text}")

if __name__ == "__main__":
    found_events = fetch_events()
    send_line_notification(found_events)
