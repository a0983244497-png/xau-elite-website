from flask import Flask, send_from_directory, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os
import pg8000.native
import urllib.parse
import requests
import json
from datetime import datetime, timedelta
import threading
import time
import yfinance as yf
from anthropic import Anthropic
from openai import OpenAI

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "xauelite2024")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_KEY = os.environ.get("OPENAI_KEY")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY")

# ─── 資料庫 ──────────────────────────────────────────────

def get_db():
    url = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        user=url.username, password=url.password,
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:]
    )

def init_db():
    conn = get_db()
    conn.run("""CREATE TABLE IF NOT EXISTS applications (
        id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL,
        telegram VARCHAR(100) NOT NULL, email VARCHAR(100) NOT NULL,
        experience VARCHAR(50), reason TEXT,
        status VARCHAR(20) DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.run("""CREATE TABLE IF NOT EXISTS daily_analysis (
        id SERIAL PRIMARY KEY, date DATE UNIQUE NOT NULL,
        direction VARCHAR(20), gold_price FLOAT, price_change FLOAT,
        bias_text TEXT, direction_text TEXT, key_levels TEXT,
        macro_text TEXT, eco_events TEXT, news_items TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.run("""CREATE TABLE IF NOT EXISTS market_articles (
        id SERIAL PRIMARY KEY, title VARCHAR(200) NOT NULL,
        content TEXT NOT NULL, xau_price FLOAT, xau_change_pct FLOAT,
        dxy_price FLOAT, dxy_change_pct FLOAT,
        is_published BOOLEAN DEFAULT TRUE,
        published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 【夥伴2】策略文章表
    conn.run("""CREATE TABLE IF NOT EXISTS strategy_articles (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        summary TEXT,
        content TEXT NOT NULL,
        raw_notes TEXT,
        category VARCHAR(50) DEFAULT 'general',
        is_published BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 【素材收集】
    conn.run("""CREATE TABLE IF NOT EXISTS feed_items (
        id SERIAL PRIMARY KEY,
        title VARCHAR(500) NOT NULL,
        summary TEXT,
        url VARCHAR(1000),
        source VARCHAR(100),
        category VARCHAR(20) DEFAULT 'trading',
        collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_read BOOLEAN DEFAULT FALSE,
        is_selected BOOLEAN DEFAULT FALSE)""")
    # 【訊號紀錄】
    conn.run("""CREATE TABLE IF NOT EXISTS signals (
        id SERIAL PRIMARY KEY,
        date DATE NOT NULL,
        direction VARCHAR(10) NOT NULL,
        entry_price FLOAT NOT NULL,
        result VARCHAR(10) NOT NULL,
        pnl_points FLOAT,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 【夥伴3】Threads 草稿表
    conn.run("""CREATE TABLE IF NOT EXISTS drafts (
        id SERIAL PRIMARY KEY,
        date DATE DEFAULT CURRENT_DATE,
        version INTEGER NOT NULL,
        style VARCHAR(50) DEFAULT 'standard',
        content TEXT NOT NULL,
        xau_price FLOAT,
        xau_change_pct FLOAT,
        status VARCHAR(20) DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

try:
    init_db()
    print("資料庫初始化成功")
except Exception as e:
    print(f"資料庫初始化失敗: {e}")

# ─── 市場數據 ─────────────────────────────────────────────

def fetch_gold_price():
    try:
        res = requests.get(f"https://finnhub.io/api/v1/quote?symbol=OANDA:XAU_USD&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        return data.get('c', 0), data.get('d', 0), data.get('dp', 0)
    except:
        return 0, 0, 0

def fetch_market_data_yfinance():
    try:
        xau = yf.Ticker("GC=F")
        xi = xau.fast_info
        xau_price = xi.last_price
        xau_prev = xi.previous_close
        xau_chg = ((xau_price - xau_prev) / xau_prev) * 100
        dxy = yf.Ticker("DX-Y.NYB")
        di = dxy.fast_info
        dxy_price = di.last_price
        dxy_prev = di.previous_close
        dxy_chg = ((dxy_price - dxy_prev) / dxy_prev) * 100
        return {"xau_price": round(xau_price,2), "xau_change_pct": round(xau_chg,2),
                "dxy_price": round(dxy_price,3), "dxy_change_pct": round(dxy_chg,3)}
    except Exception as e:
        print(f"yfinance 失敗: {e}")
        price, change, change_pct = fetch_gold_price()
        return {"xau_price": price, "xau_change_pct": change_pct, "dxy_price": 0, "dxy_change_pct": 0}

def fetch_eco_events():
    try:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        res = requests.get(f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={today}&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        return [e for e in (data.get('economicCalendar') or []) if e.get('country') in ['US','USD']][:5]
    except:
        return []

def fetch_gold_news():
    try:
        res = requests.get(f"https://finnhub.io/api/v1/news?category=forex&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        return [n for n in data if n.get('headline') and any(
            kw in n['headline'].lower() for kw in ['gold','xau','fed','dollar','inflation'])][:5]
    except:
        return []

# ─── Claude API ───────────────────────────────────────────

def get_claude():
    return Anthropic(api_key=ANTHROPIC_KEY)

def get_openai():
    return OpenAI(api_key=OPENAI_KEY)

def generate_analysis_with_claude(price, change, change_pct, eco_events, news):
    if not ANTHROPIC_KEY:
        return generate_fallback_analysis(price, change)
    eco_text = "\n".join([f"- {e.get('event','')}: 實際 {e.get('actual','—')} 預期 {e.get('estimate','—')}" for e in eco_events]) or "今日無重大數據"
    news_text = "\n".join([f"- {n.get('headline','')}" for n in news]) or "暫無重大新聞"
    prompt = f"""你是專業黃金分析師，根據以下數據生成今日分析，只回傳純 JSON：
{{
  "direction": "bullish/bearish/neutral",
  "direction_text": "多方偏向 BULLISH / 空方偏向 BEARISH / 多空觀望 NEUTRAL",
  "bias_summary": "一句話偏向（50字）",
  "bias_points": ["重點1","重點2","重點3","重點4"],
  "macro_analysis": "總經背景（150字）"
}}
數據：黃金 {price:.2f} USD，漲跌 {change:+.2f}({change_pct:+.2f}%)
總經：{eco_text}
新聞：{news_text}"""
    try:
        msg = get_claude().messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1500,
            system="只回傳純 JSON，不要 markdown。",
            messages=[{"role":"user","content":prompt}])
        text = msg.content[0].text.replace('```json','').replace('```','').strip()
        return json.loads(text)
    except Exception as e:
        print(f"Claude 分析失敗: {e}")
        return generate_fallback_analysis(price, change)

def generate_fallback_analysis(price, change):
    is_up = change > 0
    direction = "bullish" if change > 5 else "bearish" if change < -5 else "neutral"
    return {
        "direction": direction,
        "direction_text": "多方偏向 BULLISH" if direction=="bullish" else "空方偏向 BEARISH" if direction=="bearish" else "多空觀望 NEUTRAL",
        "bias_summary": f"今日黃金{'上漲' if is_up else '下跌'} {abs(change):.2f} USD，整體結構偏{'多' if is_up else '空'}方。",
        "bias_points": [
            f"整體結構偏{'多' if is_up else '空'}方，{'美元走弱' if is_up else '美元偏強'}為黃金提供{'上行' if is_up else '下行'}動能。",
            "等待 M15 整理區間形成有效突破後執行 SOP：第一根突破→第二根回測→第三根進場。",
            "重大數據公布前停手觀察，等結構重建後再進場，盤整區間內不做。",
            "停利策略：M15 區間 1:1 爬樓梯，TP1 達到後停損移至保本，M15 兩層收手。"],
        "macro_analysis": f"當前黃金 {price:.2f} USD，技術面建議等待 M15 明確突破訊號後進場，停損設於結構外，1:1 爬樓梯停利。"
    }

def generate_market_article(market_data):
    if not ANTHROPIC_KEY:
        today = datetime.now().strftime("%Y年%m月%d日")
        return {"title": f"黃金市場分析 {today}", "content": f"黃金現貨報 {market_data['xau_price']} 美元，美元指數 {market_data['dxy_price']}。"}
    xau = market_data["xau_price"]
    xau_chg = market_data["xau_change_pct"]
    dxy = market_data["dxy_price"]
    dxy_chg = market_data["dxy_change_pct"]
    today = datetime.now().strftime("%Y年%m月%d日")
    direction = "上漲" if xau_chg > 0 else "下跌"
    prompt = f"""你是 Gino 老師，用口語化、有溫度的繁體中文寫今日黃金市場分析給台灣投資人看。

【{today} 數據】
黃金：{xau} USD（{direction} {abs(xau_chg):.2f}%）
美元指數：{dxy}（{'走強' if dxy_chg>0 else '走弱'} {abs(dxy_chg):.3f}%）

要求：
- 250字以內
- Gino 講師風格：口語、短句、直接報關鍵位、結尾有互動感
- 結構：今日行情→黃金美元關係→短線看法＋關鍵價位
- 繁體中文，自然帶入 emoji（不超過2個）

格式：
標題：[標題]
正文：
[正文]"""
    try:
        msg = get_claude().messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            messages=[{"role":"user","content":prompt}])
        raw = msg.content[0].text.strip()
        title, content_lines, in_content = "", [], False
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("標題："): title = line.replace("標題：","").strip()
            elif line.startswith("正文："): in_content = True
            elif in_content and line: content_lines.append(line)
        return {"title": title or f"黃金市場分析 {today}", "content": "\n".join(content_lines) or raw}
    except Exception as e:
        print(f"文章生成失敗: {e}")
        return {"title": f"黃金市場分析 {today}", "content": f"黃金現貨報 {xau} 美元，請關注後續走勢。"}

def generate_threads_drafts(market_data):
    if not ANTHROPIC_KEY:
        return []
    xau = market_data["xau_price"]
    xau_chg = market_data["xau_change_pct"]
    dxy = market_data["dxy_price"]
    direction = "上漲" if xau_chg > 0 else "下跌"
    sup1 = round(xau - 15, 1)
    sup2 = round(xau - 35, 1)
    res1 = round(xau + 15, 1)
    res2 = round(xau + 35, 1)
    prompt = f"""你是 Gino 老師，在 Threads 分享黃金交易觀點給台灣投資人。

【今日數據】黃金：{xau} USD（今日{direction} {abs(xau_chg):.2f}%）美元指數：{dxy}
參考支撐：{sup1}、{sup2}｜參考壓力：{res1}、{res2}

風格參考（請模仿以下語氣和句型）：
「溫馨提醒：今晚注意黃金 4485、4500 這兩個位子，跌下去就不看他了🤣 剩下按照自己策略進行 謝謝各位。」
「明天黃金先看戲再決定，目前我先觀察行情有沒有反彈到 4337、4366 再決定要做多做空🙃🙃🙃」

風格規則：口語、短句、數字直接標、結尾互動句、emoji 自然帶1-2個、250字以內、繁體中文

只回傳 JSON（不要 markdown）：
{{
  "drafts": [
    {{"version": 1, "style": "hook", "content": "..."}},
    {{"version": 2, "style": "educational", "content": "..."}},
    {{"version": 3, "style": "personal", "content": "..."}}
  ]
}}

V1 hook：開頭衝擊，直接點出今日關鍵價位，結尾呼籲互動
V2 educational：用 Gino 口吻解釋黃金美元關係，帶一個操作提醒
V3 personal：第一人稱，像在和朋友說今天怎麼看這個行情"""
    try:
        msg = get_claude().messages.create(
            model="claude-sonnet-4-6", max_tokens=1000,
            system="只回傳純 JSON，不要任何 markdown 或說明文字。",
            messages=[{"role":"user","content":prompt}])
        text = msg.content[0].text.replace('```json','').replace('```','').strip()
        result = json.loads(text)
        return result.get('drafts', [])
    except Exception as e:
        print(f"草稿生成失敗: {e}")
        return []

# ─── 【夥伴2】策略筆記整理 ──────────────────────────────

def process_strategy_notes(raw_notes, category="general"):
    """
    接收雜亂的分析筆記 → Claude 整理成結構化文章
    category: timezone（時區）/ profit（停利）/ general（一般）
    """
    if not ANTHROPIC_KEY:
        return {
            "title": "策略筆記（待 AI 整理）",
            "summary": "API Key 尚未設定，以下為原始筆記內容。",
            "content": raw_notes
        }

    category_prompts = {
        "timezone": "這是關於【交易時區影響】的策略筆記，請整理成教學文章，重點說明亞洲盤/歐洲盤/美盤各時段的黃金特性與操作建議。",
        "profit": "這是關於【停利策略】的策略筆記，請整理成教學文章，重點說明如何在不同市況下設定停利、移動停損、爬樓梯停利的執行方式。",
        "general": "這是交易策略筆記，請整理成清晰的教學文章。"
    }

    category_hint = category_prompts.get(category, category_prompts["general"])

    prompt = f"""你是專業交易教學文章編輯，{category_hint}

【原始筆記】
{raw_notes}

請整理成以下 JSON 格式（只回傳 JSON，不要 markdown）：
{{
  "title": "文章標題（20字以內，吸引人）",
  "summary": "重點摘要，3-5個要點，每點一句話，用 \\n 分隔",
  "content": "完整文章內容（300-500字繁體中文，分段落，專業易懂，適合有基礎的散戶投資人）"
}}"""

    try:
        msg = get_claude().messages.create(
            model="claude-sonnet-4-20250514", max_tokens=1500,
            system="只回傳純 JSON，不要任何 markdown 或說明文字。",
            messages=[{"role":"user","content":prompt}])
        text = msg.content[0].text.replace('```json','').replace('```','').strip()
        result = json.loads(text)
        return result
    except Exception as e:
        print(f"策略整理失敗: {e}")
        return {
            "title": "策略筆記",
            "summary": "整理失敗，請重試。",
            "content": raw_notes
        }

# ─── 排程 ────────────────────────────────────────────────

def run_daily_analysis():
    print("執行每日自動分析...")
    today = datetime.utcnow().strftime('%Y-%m-%d')
    price, change, change_pct = fetch_gold_price()
    eco_events = fetch_eco_events()
    news = fetch_gold_news()
    analysis = generate_analysis_with_claude(price, change, change_pct, eco_events, news)
    try:
        conn = get_db()
        conn.run("""INSERT INTO daily_analysis
            (date,direction,gold_price,price_change,bias_text,direction_text,key_levels,macro_text,eco_events,news_items)
            VALUES (:date,:direction,:gold_price,:price_change,:bias_text,:direction_text,:key_levels,:macro_text,:eco_events,:news_items)
            ON CONFLICT (date) DO UPDATE SET
            direction=EXCLUDED.direction, gold_price=EXCLUDED.gold_price,
            price_change=EXCLUDED.price_change, bias_text=EXCLUDED.bias_text,
            direction_text=EXCLUDED.direction_text, macro_text=EXCLUDED.macro_text,
            eco_events=EXCLUDED.eco_events, news_items=EXCLUDED.news_items,
            updated_at=CURRENT_TIMESTAMP""",
            date=today, direction=analysis['direction'], gold_price=price,
            price_change=change, bias_text=json.dumps(analysis,ensure_ascii=False),
            direction_text=analysis['direction_text'], key_levels='{}',
            macro_text=analysis['macro_analysis'],
            eco_events=json.dumps(eco_events,ensure_ascii=False),
            news_items=json.dumps(news,ensure_ascii=False))
        print(f"每日分析儲存：{today}")
    except Exception as e:
        print(f"每日分析儲存失敗: {e}")

def run_partner4_article():
    print("=== 夥伴4：生成市場分析文章 ===")
    market_data = fetch_market_data_yfinance()
    article = generate_market_article(market_data)
    try:
        conn = get_db()
        conn.run("""INSERT INTO market_articles
            (title,content,xau_price,xau_change_pct,dxy_price,dxy_change_pct)
            VALUES (:title,:content,:xau_price,:xau_change_pct,:dxy_price,:dxy_change_pct)""",
            title=article["title"], content=article["content"],
            xau_price=market_data["xau_price"], xau_change_pct=market_data["xau_change_pct"],
            dxy_price=market_data["dxy_price"], dxy_change_pct=market_data["dxy_change_pct"])
        print(f"夥伴4文章儲存：{article['title']}")
    except Exception as e:
        print(f"夥伴4儲存失敗: {e}")

def run_partner3_drafts():
    print("=== 夥伴3：生成 Threads 草稿 ===")
    market_data = fetch_market_data_yfinance()
    drafts = generate_threads_drafts(market_data)
    if not drafts:
        print("夥伴3：草稿生成失敗")
        return
    try:
        conn = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        for d in drafts:
            conn.run("""INSERT INTO drafts (date,version,style,content,xau_price,xau_change_pct)
                VALUES (:date,:version,:style,:content,:xau_price,:xau_change_pct)""",
                date=today, version=d['version'], style=d['style'],
                content=d['content'], xau_price=market_data['xau_price'],
                xau_change_pct=market_data['xau_change_pct'])
        print(f"夥伴3草稿儲存：{today}，共 {len(drafts)} 篇")
    except Exception as e:
        print(f"夥伴3儲存失敗: {e}")

# ─── 素材收集爬蟲 ─────────────────────────────────────────

def fetch_finnhub_news():
    try:
        res = requests.get(
            f"https://finnhub.io/api/v1/news?category=forex&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        kw = ['gold','xau','fed','dollar','inflation','rate','treasury']
        items = [n for n in data if n.get('headline') and
                 any(k in n['headline'].lower() for k in kw)][:10]
        return [{"title": n['headline'][:500],
                 "summary": (n.get('summary') or '')[:400],
                 "url": n.get('url',''),
                 "source": "Finnhub",
                 "category": "trading"} for n in items]
    except Exception as e:
        print(f"Finnhub 新聞失敗: {e}")
        return []

def fetch_hackernews():
    try:
        import xml.etree.ElementTree as ET
        res = requests.get("https://news.ycombinator.com/rss", timeout=10,
                           headers={'User-Agent': 'Mozilla/5.0'})
        root = ET.fromstring(res.text)
        items = []
        for item in root.findall('./channel/item')[:10]:
            title = (item.findtext('title') or '').strip()
            link  = (item.findtext('link')  or '').strip()
            if title:
                items.append({"title": title[:500], "summary": "",
                              "url": link, "source": "HackerNews", "category": "life"})
        return items
    except Exception as e:
        print(f"HackerNews 失敗: {e}")
        return []

def fetch_ptt_hot():
    try:
        session = requests.Session()
        session.cookies.set('over18', '1', domain='www.ptt.cc')
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        res = session.get("https://www.ptt.cc/bbs/Gossiping/index.json",
                          timeout=10, headers=headers)
        data = res.json()
        items = []
        for post in data.get('items', [])[:15]:
            title = (post.get('title') or '').strip()
            if not title or title.startswith('Re:'):
                continue
            href = post.get('href') or post.get('link','')
            items.append({
                "title": title[:500],
                "summary": f"作者：{post.get('author','')} | 日期：{post.get('date','')}",
                "url": f"https://www.ptt.cc{href}" if href else "",
                "source": "PTT八卦",
                "category": "mix"
            })
            if len(items) >= 10:
                break
        return items
    except Exception as e:
        print(f"PTT 失敗: {e}")
        return []

def generate_threads_drafts_from_feed(market_data, feed_items_list):
    if not ANTHROPIC_KEY:
        return []
    xau = market_data["xau_price"]
    xau_chg = market_data["xau_change_pct"]
    feed_text = "\n".join([f"- [{it['source']}] {it['title']}" for it in feed_items_list[:10]])
    direction = "上漲" if xau_chg > 0 else "下跌"
    prompt = f"""你是 Gino 老師，根據以下素材和今日黃金行情，寫3個 Threads 貼文草稿。

【今日黃金】{xau} USD（{direction} {abs(xau_chg):.2f}%）

【今日素材】
{feed_text}

風格參考：
「溫馨提醒：今晚注意黃金 4485、4500 這兩個位子，跌下去就不看他了🤣 剩下按照自己策略進行 謝謝各位。」

規則：口語、短句、數字直接標、結尾互動句、emoji 自然帶1-2個、250字以內、繁體中文
可引用素材中的觀點或熱點話題，連結到黃金交易或個人成長

只回傳 JSON（不要 markdown）：
{{
  "drafts": [
    {{"version": 1, "style": "hook", "content": "..."}},
    {{"version": 2, "style": "educational", "content": "..."}},
    {{"version": 3, "style": "personal", "content": "..."}}
  ]
}}"""
    try:
        msg = get_claude().messages.create(
            model="claude-sonnet-4-6", max_tokens=1000,
            system="只回傳純 JSON，不要任何 markdown 或說明文字。",
            messages=[{"role":"user","content":prompt}])
        text = msg.content[0].text.replace('```json','').replace('```','').strip()
        result = json.loads(text)
        return result.get('drafts', [])
    except Exception as e:
        print(f"素材草稿生成失敗: {e}")
        return []

def run_feed_collection():
    print("=== 素材收集：開始 ===")
    all_items = []
    all_items.extend(fetch_finnhub_news())
    all_items.extend(fetch_hackernews())
    all_items.extend(fetch_ptt_hot())
    if not all_items:
        print("素材收集：無結果")
        return 0
    try:
        conn = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        existing = conn.run(
            "SELECT url FROM feed_items WHERE collected_at::date=:today AND url IS NOT NULL",
            today=today)
        existing_urls = {r[0] for r in existing}
        count = 0
        for item in all_items:
            url = (item.get('url') or '')[:1000]
            if url and url in existing_urls:
                continue
            conn.run("""INSERT INTO feed_items (title,summary,url,source,category)
                VALUES (:title,:summary,:url,:source,:category)""",
                title=item['title'][:500],
                summary=(item.get('summary') or '')[:500],
                url=url,
                source=(item.get('source') or '')[:100],
                category=item.get('category','trading'))
            if url:
                existing_urls.add(url)
            count += 1
        print(f"素材收集完成：{count} 篇新文章")
        return count
    except Exception as e:
        print(f"素材儲存失敗: {e}")
        return 0

_last_run = {}

def _ran_today(job):
    return _last_run.get(job) == datetime.utcnow().strftime('%Y-%m-%d')

def _mark_ran(job):
    _last_run[job] = datetime.utcnow().strftime('%Y-%m-%d')

def scheduler():
    while True:
        try:
            now = datetime.utcnow()
            h, m, wd = now.hour, now.minute, now.weekday()

            # UTC 00:00–00:04 = 台北時間 08:00（夥伴3/夥伴4）
            if h == 0 and m < 5:
                if not _ran_today('partner3'):
                    _mark_ran('partner3')
                    threading.Thread(target=run_partner3_drafts, daemon=True).start()
                    print("排程啟動：夥伴3")
                if wd in (0, 2, 4) and not _ran_today('partner4'):
                    _mark_ran('partner4')
                    threading.Thread(target=run_partner4_article, daemon=True).start()
                    print("排程啟動：夥伴4")

            # UTC 23:00–23:04 = 台北時間 07:00（素材收集，每天）
            if h == 23 and m < 5:
                if not _ran_today('feed_collection'):
                    _mark_ran('feed_collection')
                    threading.Thread(target=run_feed_collection, daemon=True).start()
                    print("排程啟動：素材收集")

            # UTC 22:00–22:04 = 台北時間 06:00（每日結構分析，週一至週五）
            if h == 22 and m < 5 and wd < 5:
                if not _ran_today('daily_analysis'):
                    _mark_ran('daily_analysis')
                    threading.Thread(target=run_daily_analysis, daemon=True).start()
                    print("排程啟動：每日分析")

        except Exception as e:
            print(f"排程錯誤: {e}")

        time.sleep(60)

scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()

# ─── API 路由 ─────────────────────────────────────────────

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/health')
def health(): return jsonify({"status": "running"})

@app.route('/api/test-claude')
def test_claude():
    if not ANTHROPIC_KEY: return jsonify({"ok":False,"error":"找不到 ANTHROPIC_API_KEY"}),500
    try:
        msg = get_claude().messages.create(model="claude-haiku-4-5-20251001", max_tokens=100,
            messages=[{"role":"user","content":"請用繁體中文回一句話：Claude 連線測試成功"}])
        return jsonify({"ok":True,"reply":msg.content[0].text})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/generate_analysis', methods=['POST'])
def generate_analysis():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    try:
        run_daily_analysis()
        return jsonify({"ok":True,"message":"分析已生成並儲存"})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/daily_analysis', methods=['GET'])
def get_daily_analysis():
    date = request.args.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    try:
        conn = get_db()
        rows = conn.run("SELECT * FROM daily_analysis WHERE date = :date", date=date)
        if not rows:
            price, change, change_pct = fetch_gold_price()
            eco_events = fetch_eco_events()
            news = fetch_gold_news()
            analysis = generate_analysis_with_claude(price, change, change_pct, eco_events, news)
            return jsonify({"ok":True,"live":True,"date":date,"gold_price":price,"price_change":change,
                "direction":analysis['direction'],"direction_text":analysis['direction_text'],
                "bias_summary":analysis['bias_summary'],"bias_points":analysis['bias_points'],
                "macro_text":analysis['macro_analysis'],"eco_events":eco_events,"news_items":news})
        cols = ['id','date','direction','gold_price','price_change','bias_text','direction_text',
                'key_levels','macro_text','eco_events','news_items','created_at','updated_at']
        row = dict(zip(cols, rows[0]))
        bias = json.loads(row['bias_text']) if row['bias_text'] else {}
        return jsonify({"ok":True,"live":False,"date":str(row['date']),"gold_price":row['gold_price'],
            "price_change":row['price_change'],"direction":row['direction'],"direction_text":row['direction_text'],
            "bias_summary":bias.get('bias_summary',''),"bias_points":bias.get('bias_points',[]),
            "macro_text":row['macro_text'],
            "eco_events":json.loads(row['eco_events']) if row['eco_events'] else [],
            "news_items":json.loads(row['news_items']) if row['news_items'] else []})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/gold_price', methods=['GET'])
def gold_price():
    price, change, change_pct = fetch_gold_price()
    return jsonify({"ok":True,"price":price,"change":change,"change_pct":change_pct})

# ─── 夥伴4 API ────────────────────────────────────────────

@app.route('/api/articles', methods=['GET'])
def get_articles():
    page = request.args.get('page',1,type=int)
    per_page = request.args.get('per_page',10,type=int)
    offset = (page-1)*per_page
    try:
        conn = get_db()
        rows = conn.run("""SELECT id,title,content,xau_price,xau_change_pct,dxy_price,dxy_change_pct,published_at
            FROM market_articles WHERE is_published=TRUE
            ORDER BY published_at DESC LIMIT :limit OFFSET :offset""", limit=per_page, offset=offset)
        count = conn.run("SELECT COUNT(*) FROM market_articles WHERE is_published=TRUE")[0][0]
        cols = ['id','title','content','xau_price','xau_change_pct','dxy_price','dxy_change_pct','published_at']
        articles = [dict(zip(cols,r)) for r in rows]
        for a in articles: a['published_at'] = str(a['published_at'])
        return jsonify({"ok":True,"articles":articles,"total":count,"pages":(count+per_page-1)//per_page,"current_page":page})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/articles/latest', methods=['GET'])
def get_latest_article():
    try:
        conn = get_db()
        rows = conn.run("""SELECT id,title,content,xau_price,xau_change_pct,dxy_price,dxy_change_pct,published_at
            FROM market_articles WHERE is_published=TRUE ORDER BY published_at DESC LIMIT 1""")
        if not rows: return jsonify({"ok":False,"error":"尚無文章"}),404
        cols = ['id','title','content','xau_price','xau_change_pct','dxy_price','dxy_change_pct','published_at']
        article = dict(zip(cols,rows[0]))
        article['published_at'] = str(article['published_at'])
        return jsonify({"ok":True,"article":article})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/articles/trigger', methods=['POST'])
def trigger_partner4():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    try:
        run_partner4_article()
        conn = get_db()
        rows = conn.run("SELECT id,title,published_at FROM market_articles ORDER BY published_at DESC LIMIT 1")
        latest = {"id":rows[0][0],"title":rows[0][1],"published_at":str(rows[0][2])} if rows else None
        return jsonify({"ok":True,"message":"夥伴4任務完成","article":latest})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── 【夥伴3】Threads 草稿 API ───────────────────────────

@app.route('/api/drafts', methods=['GET'])
def get_drafts():
    date = request.args.get('date')
    try:
        conn = get_db()
        if date:
            rows = conn.run("""SELECT id,date,version,style,content,xau_price,xau_change_pct,status,created_at
                FROM drafts WHERE date=:date ORDER BY version""", date=date)
        else:
            rows = conn.run("""SELECT id,date,version,style,content,xau_price,xau_change_pct,status,created_at
                FROM drafts ORDER BY created_at DESC LIMIT 30""")
        cols = ['id','date','version','style','content','xau_price','xau_change_pct','status','created_at']
        drafts_list = [dict(zip(cols,r)) for r in rows]
        for d in drafts_list:
            d['date'] = str(d['date'])
            d['created_at'] = str(d['created_at'])
        return jsonify({"ok":True,"drafts":drafts_list})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/drafts/latest', methods=['GET'])
def get_latest_drafts():
    try:
        conn = get_db()
        rows = conn.run("""SELECT id,date,version,style,content,xau_price,xau_change_pct,status,created_at
            FROM drafts WHERE date=(SELECT MAX(date) FROM drafts) ORDER BY version""")
        if not rows:
            return jsonify({"ok":False,"error":"尚無草稿"}),404
        cols = ['id','date','version','style','content','xau_price','xau_change_pct','status','created_at']
        drafts_list = [dict(zip(cols,r)) for r in rows]
        for d in drafts_list:
            d['date'] = str(d['date'])
            d['created_at'] = str(d['created_at'])
        return jsonify({"ok":True,"drafts":drafts_list,"date":drafts_list[0]['date']})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/drafts/trigger', methods=['POST'])
def trigger_partner3():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    try:
        run_partner3_drafts()
        conn = get_db()
        rows = conn.run("""SELECT id,date,version,style,content,status,created_at
            FROM drafts WHERE date=(SELECT MAX(date) FROM drafts) ORDER BY version""")
        cols = ['id','date','version','style','content','status','created_at']
        drafts_list = [dict(zip(cols,r)) for r in rows]
        for d in drafts_list:
            d['date'] = str(d['date'])
            d['created_at'] = str(d['created_at'])
        return jsonify({"ok":True,"message":"夥伴3任務完成","drafts":drafts_list})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/drafts/<int:draft_id>', methods=['PATCH'])
def update_draft(draft_id):
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    status = request.json.get('status')
    if status not in ['approved','rejected','pending']:
        return jsonify({"ok":False,"error":"無效狀態"}),400
    try:
        conn = get_db()
        conn.run("UPDATE drafts SET status=:status WHERE id=:id", status=status, id=draft_id)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── 【夥伴2】策略文章 API ────────────────────────────────

@app.route('/api/strategy', methods=['POST'])
def create_strategy_article():
    """
    接收分析筆記 → Claude 整理 → 存入資料庫
    Body: { "notes": "...", "category": "timezone/profit/general" }
    """
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401

    data = request.json
    raw_notes = data.get('notes','').strip()
    category = data.get('category','general')

    if not raw_notes:
        return jsonify({"ok":False,"error":"請輸入筆記內容"}),400

    try:
        # Claude 整理筆記
        result = process_strategy_notes(raw_notes, category)

        # 存入資料庫
        conn = get_db()
        rows = conn.run("""INSERT INTO strategy_articles
            (title, summary, content, raw_notes, category)
            VALUES (:title, :summary, :content, :raw_notes, :category)
            RETURNING id, created_at""",
            title=result['title'], summary=result['summary'],
            content=result['content'], raw_notes=raw_notes, category=category)

        article_id = rows[0][0]
        created_at = str(rows[0][1])

        return jsonify({
            "ok": True,
            "message": "策略文章已生成並儲存",
            "article": {
                "id": article_id,
                "title": result['title'],
                "summary": result['summary'],
                "content": result['content'],
                "category": category,
                "created_at": created_at
            }
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/strategy/articles', methods=['GET'])
def get_strategy_articles():
    """取得策略文章列表，可用 category 篩選"""
    category = request.args.get('category')
    try:
        conn = get_db()
        if category:
            rows = conn.run("""SELECT id,title,summary,content,category,created_at
                FROM strategy_articles WHERE is_published=TRUE AND category=:cat
                ORDER BY created_at DESC""", cat=category)
        else:
            rows = conn.run("""SELECT id,title,summary,content,category,created_at
                FROM strategy_articles WHERE is_published=TRUE
                ORDER BY created_at DESC""")
        cols = ['id','title','summary','content','category','created_at']
        articles = [dict(zip(cols,r)) for r in rows]
        for a in articles: a['created_at'] = str(a['created_at'])
        return jsonify({"ok":True,"articles":articles})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/strategy/articles/<int:article_id>', methods=['DELETE'])
def delete_strategy_article(article_id):
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    try:
        conn = get_db()
        conn.run("UPDATE strategy_articles SET is_published=FALSE WHERE id=:id", id=article_id)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── 申請 API ────────────────────────────────────────────

@app.route('/api/apply', methods=['POST'])
def apply():
    data = request.json
    name = data.get('name','').strip()
    telegram = data.get('telegram','').strip()
    email = data.get('email','').strip()
    experience = data.get('experience','').strip()
    reason = data.get('reason','').strip()
    if not name or not telegram or not email:
        return jsonify({"ok":False,"error":"請填入所有必填欄位"}),400
    try:
        conn = get_db()
        conn.run("INSERT INTO applications (name,telegram,email,experience,reason) VALUES (:name,:telegram,:email,:experience,:reason)",
            name=name, telegram=telegram, email=email, experience=experience, reason=reason)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/applications', methods=['GET'])
def get_applications():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    try:
        conn = get_db()
        rows = conn.run("SELECT * FROM applications ORDER BY created_at DESC")
        cols = ['id','name','telegram','email','experience','reason','status','created_at']
        return jsonify({"ok":True,"data":[dict(zip(cols,r)) for r in rows]})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/applications/<int:app_id>', methods=['PATCH'])
def update_application(app_id):
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    status = request.json.get('status')
    if status not in ['approved','rejected','pending']:
        return jsonify({"ok":False,"error":"無效狀態"}),400
    try:
        conn = get_db()
        conn.run("UPDATE applications SET status=:status WHERE id=:id", status=status, id=app_id)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── 【訊號紀錄】API ──────────────────────────────────────

@app.route('/api/signals', methods=['GET'])
def get_signals():
    limit = request.args.get('limit', 20, type=int)
    try:
        conn = get_db()
        rows = conn.run("""SELECT id,date,direction,entry_price,result,pnl_points,note,created_at
            FROM signals ORDER BY date DESC, created_at DESC LIMIT :limit""", limit=limit)
        cols = ['id','date','direction','entry_price','result','pnl_points','note','created_at']
        sigs = [dict(zip(cols,r)) for r in rows]
        for s in sigs:
            s['date'] = str(s['date'])
            s['created_at'] = str(s['created_at'])
        return jsonify({"ok":True,"signals":sigs})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/signals', methods=['POST'])
def add_signal():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    data = request.json
    date = data.get('date','').strip()
    direction = data.get('direction','').strip()
    entry_price = data.get('entry_price')
    result = data.get('result','').strip()
    pnl_points = data.get('pnl_points')
    note = data.get('note','').strip()
    if not date or direction not in ('long','short') or not entry_price or result not in ('win','loss'):
        return jsonify({"ok":False,"error":"請填入日期、方向、進場價、結果"}),400
    try:
        conn = get_db()
        rows = conn.run("""INSERT INTO signals (date,direction,entry_price,result,pnl_points,note)
            VALUES (:date,:direction,:entry_price,:result,:pnl_points,:note) RETURNING id,created_at""",
            date=date, direction=direction, entry_price=float(entry_price),
            result=result, pnl_points=float(pnl_points) if pnl_points is not None else None, note=note)
        return jsonify({"ok":True,"id":rows[0][0],"created_at":str(rows[0][1])})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/signals/stats', methods=['GET'])
def get_signal_stats():
    try:
        conn = get_db()
        rows = conn.run("SELECT COUNT(*),SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),AVG(pnl_points) FROM signals")
        total, wins, avg_pnl = rows[0]
        total = total or 0; wins = wins or 0
        win_rate = round((wins/total)*100, 1) if total > 0 else 0
        return jsonify({"ok":True,"total":total,"wins":wins,"losses":total-wins,
            "win_rate":win_rate,"avg_pnl":round(avg_pnl,1) if avg_pnl else 0})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ─── 圖表串流分析 ────────────────────────────────────────

ANALYZER_PROMPT = """你是 XAU Elite 的黃金交易分析師，請分析這張 XAU/USD 圖表。

用繁體中文輸出以下格式，不要 markdown 符號：

【方向判斷】
（只寫一行：多方偏向 BULLISH ▲ 或 空方偏向 BEARISH ▼ 或 多空觀望 NEUTRAL ◉）
（第二行：一句話說明根據什麼判斷，口語化）

【關鍵位一覽】
• 上方壓力：$XXX（若有多個用 / 分隔）
• 整理區間：$XXX — $XXX
• 下方支撐：$XXX（若有多個用 / 分隔）

【破框SOP建議】
1. 等待 M15 整理區間明確形成
2. 第一根K棒突破關鍵位，確認方向
3. 第二根K棒回測守住（不跌回 / 不反彈回區間）
4. 第三根K棒確認後進場，停損設於結構外

【風險提示】
• （2-3條口語化提示，不用術語，訪客看得懂）

要求：口語化、簡潔、不用艱深術語、讓完全沒有交易經驗的人也能看懂。"""

@app.route('/api/analyzer/image', methods=['POST'])
def analyze_image():
    if not ANTHROPIC_KEY:
        return jsonify({"ok": False, "error": "Claude API 未設定"}), 500
    data = request.json or {}
    raw_image = data.get('image', '')
    media_type = data.get('media_type', 'image/jpeg')
    if not raw_image:
        return jsonify({"ok": False, "error": "未收到圖片"}), 400
    # Strip data URL prefix if present
    image_b64 = raw_image.split(',', 1)[1] if ',' in raw_image else raw_image

    def generate():
        try:
            with get_claude().messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64",
                                    "media_type": media_type,
                                    "data": image_b64}},
                        {"type": "text", "text": ANALYZER_PROMPT}
                    ]
                }]
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

# ─── 新版圖表分析 + 對話 ──────────────────────────────────

ANALYZE_CHART_PROMPT = """你是黃金交易分析師，分析這張 XAU/USD 圖表。用繁體中文輸出，嚴格按以下格式，不加任何 markdown 符號或多餘標點。

【方向】
（只寫一行：多方偏向 BULLISH ▲ 或 空方偏向 BEARISH ▼ 或 多空觀望 NEUTRAL ◉）
（第二行：一句口語說明判斷依據）

【關鍵位】
（從圖表識別實際數字填入，禁止使用 XXXX 佔位符）
（五個標籤順序固定如下，只填數字，不加 $ 或單位）

上方大壓：（圖表上方主要阻力區價位）
TP1 目標：（第一獲利目標）
進場觸發：（突破或跌破此位才進場）
停損基準：（進場後的止損位）
下方大撐：（圖表下方主要支撐區價位）

價位邏輯規則（必須遵守）：
- 多方偏向：上方大壓 > TP1 目標 > 進場觸發 > 停損基準 > 下方大撐
- 空方偏向：上方大壓 > 停損基準 > 進場觸發 > TP1 目標 > 下方大撐
- 觀望：依圖表填入合理撐壓位，TP1 目標與停損基準標注 待確認

【破框SOP】
（四步驟，帶入具體價位數字，口語化）
1. 等 M15 整理區間確認，關鍵突破位為 [進場觸發價位]
2. 第一根 K 棒突破 [進場觸發價位]，確認收盤站穩（多）或跌破（空）
3. 第二根 K 棒回測 [進場觸發價位] 守住，不跌回（多）或不漲回（空）
4. 第三根 K 棒確認方向後進場，目標 TP1 [TP1價位]，停損設於 [停損基準價位]

【風險提示】
• （口語，不用術語，訪客看得懂）
• （口語）
• （口語）

【不做單條件】
• 若圖表有明確不應進場的情況則列出；無特殊警示則省略整段

要求：口語、簡潔，讓沒有交易經驗的人也能看懂。"""


@app.route('/api/analyze-chart', methods=['POST'])
def analyze_chart():
    if not OPENAI_KEY:
        return jsonify({"ok": False, "error": "OpenAI API 未設定"}), 500
    data = request.json or {}
    raw_image = data.get('image', '')
    media_type = data.get('media_type', 'image/jpeg')
    if not raw_image:
        return jsonify({"ok": False, "error": "未收到圖片"}), 400
    # Keep full data URL for OpenAI (it accepts data:image/...;base64,... format)
    image_url = raw_image if raw_image.startswith('data:') else f"data:{media_type};base64,{raw_image}"

    def generate():
        chunks = []
        try:
            stream = get_openai().chat.completions.create(
                model="gpt-4o",
                max_tokens=1000,
                stream=True,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": ANALYZE_CHART_PROMPT}
                    ]
                }]
            )
            for chunk in stream:
                text = chunk.choices[0].delta.content or ''
                if text:
                    chunks.append(text)
                    yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
            full = ''.join(chunks)
            print(f"\n=== analyze-chart 完整回應 ===\n{full}\n=== END ===\n", flush=True)
            yield "data: [DONE]\n\n"
        except Exception as e:
            print(f"analyze-chart 錯誤: {e}", flush=True)
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/api/chat-chart', methods=['POST'])
def chat_chart():
    if not ANTHROPIC_KEY:
        return jsonify({"ok": False, "error": "Claude API 未設定"}), 500
    data = request.json or {}
    raw_image = data.get('image', '')
    media_type = data.get('media_type', 'image/jpeg')
    analysis = data.get('analysis', '')
    history = data.get('history', [])
    message = data.get('message', '').strip()
    if not message:
        return jsonify({"ok": False, "error": "未收到訊息"}), 400
    image_b64 = raw_image.split(',', 1)[1] if ',' in raw_image else raw_image

    # Image in first turn so model retains visual context throughout chat
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": media_type or "image/jpeg",
                            "data": image_b64}},
                {"type": "text", "text": "這是一張 XAU/USD 圖表。"}
            ]
        },
        {
            "role": "assistant",
            "content": f"好的，我已經分析了這張圖表：\n\n{analysis}"
        }
    ]
    for msg in history:
        messages.append({
            "role": msg.get("role"),
            "content": msg.get("content", "")
        })
    messages.append({"role": "user", "content": message})

    def generate():
        try:
            with get_claude().messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=600,
                system="你是 XAU Elite 的黃金交易分析師助手，用繁體中文回答，口語化、簡潔、重點直接講。",
                messages=messages
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# ─── 【素材收集】Feed API ─────────────────────────────────

@app.route('/api/feed', methods=['GET'])
def get_feed():
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    category = request.args.get('category')
    try:
        conn = get_db()
        if category:
            rows = conn.run("""SELECT id,title,summary,url,source,category,
                collected_at,is_read,is_selected
                FROM feed_items WHERE collected_at::date=:date AND category=:cat
                ORDER BY is_selected DESC, collected_at DESC""", date=date, cat=category)
        else:
            rows = conn.run("""SELECT id,title,summary,url,source,category,
                collected_at,is_read,is_selected
                FROM feed_items WHERE collected_at::date=:date
                ORDER BY is_selected DESC, collected_at DESC""", date=date)
        cols = ['id','title','summary','url','source','category',
                'collected_at','is_read','is_selected']
        items = [dict(zip(cols,r)) for r in rows]
        for it in items:
            it['collected_at'] = str(it['collected_at'])
        return jsonify({"ok":True,"items":items,"date":date,"count":len(items)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/feed/collect', methods=['POST'])
def trigger_feed_collect():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    try:
        count = run_feed_collection()
        return jsonify({"ok":True,"count":count})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/feed/<int:item_id>/select', methods=['PATCH'])
def select_feed_item(item_id):
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    selected = request.json.get('is_selected', True)
    try:
        conn = get_db()
        conn.run("UPDATE feed_items SET is_selected=:sel WHERE id=:id",
                 sel=selected, id=item_id)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/api/feed/to-draft', methods=['POST'])
def feed_to_draft():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok":False,"error":"未授權"}),401
    data_req = request.json or {}
    item_ids = data_req.get('item_ids', [])
    date = data_req.get('date', datetime.now().strftime('%Y-%m-%d'))
    try:
        conn = get_db()
        if item_ids:
            rows = []
            for iid in item_ids[:15]:
                r = conn.run("""SELECT id,title,summary,url,source,category
                    FROM feed_items WHERE id=:id""", id=int(iid))
                if r:
                    rows.extend(r)
        else:
            rows = conn.run("""SELECT id,title,summary,url,source,category
                FROM feed_items WHERE collected_at::date=:date AND is_selected=TRUE
                LIMIT 15""", date=date)
        cols = ['id','title','summary','url','source','category']
        items = [dict(zip(cols,r)) for r in rows]
        if not items:
            return jsonify({"ok":False,"error":"沒有選取的素材"}),400
        market_data = fetch_market_data_yfinance()
        drafts = generate_threads_drafts_from_feed(market_data, items)
        if not drafts:
            return jsonify({"ok":False,"error":"草稿生成失敗"}),500
        today = datetime.now().strftime('%Y-%m-%d')
        saved = []
        for d in drafts:
            r = conn.run("""INSERT INTO drafts
                (date,version,style,content,xau_price,xau_change_pct)
                VALUES (:date,:version,:style,:content,:xau,:xau_chg)
                RETURNING id""",
                date=today, version=d['version'], style=d['style'],
                content=d['content'], xau=market_data['xau_price'],
                xau_chg=market_data['xau_change_pct'])
            saved.append({"id":r[0][0],"version":d['version'],
                          "style":d['style'],"content":d['content']})
        return jsonify({"ok":True,"message":f"已生成 {len(saved)} 個草稿","drafts":saved})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route('/<path:path>')
def serve_file(path): return send_from_directory('.', path)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
