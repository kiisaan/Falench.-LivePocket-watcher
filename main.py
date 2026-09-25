import os
import requests
from bs4 import BeautifulSoup
import re

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

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

def fetch_falench_events(notified_urls):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    })

    # トップページに事前アクセスしてCookie・セッションを確立
    try:
        session.get("https://livepocket.jp/", timeout=10)
    except Exception as e:
        print(f"[WARN] セッション初期化失敗: {e}")

    new_events = []
    seen_urls = set()

    # 最大3ページまで検索結果を巡回
    for page in range(1, 4):
        search_url = f"https://livepocket.jp/event/search?search_word=Falench&page={page}"
        print(f"[DEBUG] 検索ページを取得中 (Page {page}): {search_url}")
        
        try:
            res = session.get(search_url, timeout=15)
            res.raise_for_status()
        except Exception as e:
            print(f"[ERROR] ページ取得エラー (Page {page}): {e}")
            break

        soup = BeautifulSoup(res.text, "html.parser")
        
        # LivePocketの検索結果カード要素を取得（複数のHTML構造に対応）
        cards = soup.select(".event-card, .search-item, .event-list-item, li, article, .box-event")
        
        # カード要素が特定できない場合は /e/ または /event/detail/ を含むaタグを直接取得
        if not cards:
            cards = soup.find_all("a", href=True)

        found_in_page = 0

        for card in cards:
            a_tag = card if card.name == "a" else card.find("a", href=True)
            if not a_tag or not a_tag.get("href"):
                continue
                
            href = a_tag["href"]
            if "/e/" not in href and "/event/detail/" not in href:
                continue
                
            full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
            clean_url = full_url.split("?")[0]
            
            if "/event/search" in clean_url or clean_url in seen_urls:
                continue

            card_text = card.get_text(separator=" ", strip=True)
            
            # 「falench」という文字（大文字・小文字不問）が含まれているかチェック
            if "falench" not in card_text.lower():
                continue

            seen_urls.add(clean_url)
            found_in_page += 1

            if clean_url in notified_urls:
                print(f"[SKIP] 通知済みのためスキップ: {clean_url}")
                continue

            # タイトルの整形
            title = a_tag.get_text(strip=True) or card_text[:50]
            # 改行や連続スペースを整形
            title = re.sub(r'\s+', ' ', title).strip()
            if len(title) > 70:
                title = title[:70] + "..."
                
            print(f"[MATCH] 該当ライブを発見: {title} ({clean_url})")
            new_events.append({"title": title, "url": clean_url})

        # ページ内に該当カードが0件になったら巡回終了
        if found_in_page == 0 and page > 1:
            break

    print(f"[DEBUG] 抽出された「Falench.」出演ライブ合計数: {len(new_events)}")
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
