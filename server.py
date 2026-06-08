from flask import Flask, send_from_directory, request, jsonify
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

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "xauelite2024")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
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
    prompt = f"""你是專業貴金屬分析師，撰寫給台灣投資人的市場分析文章。
【{today} 數據】黃金：{xau} USD（{'上漲' if xau_chg>0 else '下跌'} {abs(xau_chg):.2f}%）美元指數：{dxy}（{'走強' if dxy_chg>0 else '走弱'} {abs(dxy_chg):.3f}%）
要求：200-250字繁體中文，結構：行情→相關性分析→短線觀點，最後附支撐或壓力價位。
格式：
標題：[標題]
正文：
[正文]"""
    try:
        msg = get_claude().messages.create(
            model="claude-sonnet-4-20250514", max_tokens=600,
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

def scheduler():
    while True:
        now_utc = datetime.utcnow()
        weekday = now_utc.weekday()
        jobs_today = []
        if weekday < 5:
            t_daily = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
            if now_utc < t_daily: jobs_today.append((t_daily, "daily_analysis"))
        if weekday in (0, 2, 4):
            t_p4 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            if now_utc < t_p4: jobs_today.append((t_p4, "partner4"))
        if jobs_today:
            jobs_today.sort(key=lambda x: x[0])
            next_time, job_name = jobs_today[0]
            wait_sec = (next_time - now_utc).total_seconds()
            print(f"下次任務：{job_name} @ {next_time} UTC")
            time.sleep(max(wait_sec, 1))
            if job_name == "daily_analysis": run_daily_analysis()
            elif job_name == "partner4": run_partner4_article()
        else:
            tomorrow = (now_utc + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            time.sleep(max((tomorrow - now_utc).total_seconds(), 60))

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

@app.route('/<path:path>')
def serve_file(path): return send_from_directory('.', path)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
