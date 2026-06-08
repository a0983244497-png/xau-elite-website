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

TZ_OFFSET = 8  # 台灣時間 UTC+8


# ─── 資料庫連線 ──────────────────────────────────────────────

def get_db():
    url = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        user=url.username, password=url.password,
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:]
    )


def init_db():
    conn = get_db()

    # 原有：申請表
    conn.run("""
        CREATE TABLE IF NOT EXISTS applications (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            telegram VARCHAR(100) NOT NULL,
            email VARCHAR(100) NOT NULL,
            experience VARCHAR(50),
            reason TEXT,
            status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 原有：每日分析表
    conn.run("""
        CREATE TABLE IF NOT EXISTS daily_analysis (
            id SERIAL PRIMARY KEY,
            date DATE UNIQUE NOT NULL,
            direction VARCHAR(20),
            gold_price FLOAT,
            price_change FLOAT,
            bias_text TEXT,
            direction_text TEXT,
            key_levels TEXT,
            macro_text TEXT,
            eco_events TEXT,
            news_items TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 【夥伴4新增】市場分析文章表（週一三五 08:00 自動生成）
    conn.run("""
        CREATE TABLE IF NOT EXISTS market_articles (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            content TEXT NOT NULL,
            xau_price FLOAT,
            xau_change_pct FLOAT,
            dxy_price FLOAT,
            dxy_change_pct FLOAT,
            is_published BOOLEAN DEFAULT TRUE,
            published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


try:
    init_db()
    print("資料庫初始化成功")
except Exception as e:
    print(f"資料庫初始化失敗: {e}")


# ─── 市場數據抓取 ────────────────────────────────────────────

def fetch_gold_price():
    """原有：用 Finnhub 抓黃金價格（給 daily_analysis 用）"""
    try:
        res = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol=OANDA:XAU_USD&token={FINNHUB_KEY}",
            timeout=10
        )
        data = res.json()
        return data.get('c', 0), data.get('d', 0), data.get('dp', 0)
    except:
        return 0, 0, 0


def fetch_market_data_yfinance():
    """
    【夥伴4新增】用 yfinance 抓黃金 + 美元指數
    免費，不需額外 API Key
    Returns: dict { xau_price, xau_change_pct, dxy_price, dxy_change_pct }
    """
    try:
        # 黃金期貨（最接近現貨）
        xau = yf.Ticker("GC=F")
        xau_info = xau.fast_info
        xau_price = xau_info.last_price
        xau_prev = xau_info.previous_close
        xau_chg_pct = ((xau_price - xau_prev) / xau_prev) * 100

        # 美元指數
        dxy = yf.Ticker("DX-Y.NYB")
        dxy_info = dxy.fast_info
        dxy_price = dxy_info.last_price
        dxy_prev = dxy_info.previous_close
        dxy_chg_pct = ((dxy_price - dxy_prev) / dxy_prev) * 100

        return {
            "xau_price": round(xau_price, 2),
            "xau_change_pct": round(xau_chg_pct, 2),
            "dxy_price": round(dxy_price, 3),
            "dxy_change_pct": round(dxy_chg_pct, 3),
        }
    except Exception as e:
        print(f"yfinance 抓取失敗: {e}")
        # fallback：用 Finnhub 黃金，DXY 設為 N/A
        price, change, change_pct = fetch_gold_price()
        return {
            "xau_price": price,
            "xau_change_pct": change_pct,
            "dxy_price": 0,
            "dxy_change_pct": 0,
        }


def fetch_eco_events():
    try:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        res = requests.get(
            f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={today}&token={FINNHUB_KEY}",
            timeout=10
        )
        data = res.json()
        events = [e for e in (data.get('economicCalendar') or [])
                  if e.get('country') in ['US', 'USD']][:5]
        return events
    except:
        return []


def fetch_gold_news():
    try:
        res = requests.get(
            f"https://finnhub.io/api/v1/news?category=forex&token={FINNHUB_KEY}",
            timeout=10
        )
        data = res.json()
        news = [n for n in data if n.get('headline') and any(
            kw in n['headline'].lower() for kw in ['gold', 'xau', 'fed', 'dollar', 'inflation']
        )][:5]
        return news
    except:
        return []


# ─── Claude API ──────────────────────────────────────────────

def generate_analysis_with_claude(price, change, change_pct, eco_events, news):
    """原有：給 daily_analysis 用的 Claude 分析（haiku）"""
    if not ANTHROPIC_KEY:
        return generate_fallback_analysis(price, change)

    eco_text = "\n".join([
        f"- {e.get('event','')}: 實際 {e.get('actual','—')} 預期 {e.get('estimate','—')} 前值 {e.get('prev','—')}"
        for e in eco_events
    ]) or "今日無重大數據"

    news_text = "\n".join([f"- {n.get('headline','')}" for n in news]) or "暫無重大新聞"

    prompt = f"""你是一位專業的黃金現貨（XAU/USD）交易分析師，請根據以下數據生成今日日內分析。

【當前數據】
- 黃金現價：{price:.2f} USD
- 今日漲跌：{change:+.2f} USD ({change_pct:+.2f}%)
- 分析時間：台灣時間早上 06:00

【今日總經事件】
{eco_text}

【最新市場新聞】
{news_text}

請生成以下分析，格式為 JSON，只回傳 JSON 不要其他文字：
{{
  "direction": "bullish 或 bearish 或 neutral",
  "direction_text": "多方偏向 BULLISH 或 空方偏向 BEARISH 或 多空觀望 NEUTRAL",
  "bias_summary": "一句話說明今日偏向（約50字）",
  "bias_points": [
    "重點1（約30字，說明整體方向）",
    "重點2（約30字，說明M15進場條件）",
    "重點3（約30字，說明風險注意事項）",
    "重點4（約30字，說明停利策略）"
  ],
  "macro_analysis": "總經背景分析（約150字，包含Fed政策、美元走勢、技術面總結）"
}}

注意：繁體中文，分析具體專業，技術面結合破框SOP邏輯。"""

    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system="你只能回傳純 JSON 物件，不要任何說明文字，不要 markdown 標記（不要 ```）。",
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text
        text = text.replace('```json', '').replace('```', '').strip()
        return json.loads(text)
    except Exception as e:
        print(f"Claude API 錯誤: {e}")
        return generate_fallback_analysis(price, change)


def generate_fallback_analysis(price, change):
    is_up = change > 0
    direction = "bullish" if change > 5 else "bearish" if change < -5 else "neutral"
    direction_text = "多方偏向 BULLISH" if direction == "bullish" else "空方偏向 BEARISH" if direction == "bearish" else "多空觀望 NEUTRAL"
    return {
        "direction": direction,
        "direction_text": direction_text,
        "bias_summary": f"今日黃金{'上漲' if is_up else '下跌'} {abs(change):.2f} USD，整體結構偏{'多' if is_up else '空'}方，關注 M15 突破訊號。",
        "bias_points": [
            f"整體結構偏{'多' if is_up else '空'}方，{'美元走弱' if is_up else '美元偏強'}為黃金提供{'上行' if is_up else '下行'}動能。",
            "等待 M15 整理區間形成有效突破後執行 SOP：第一根突破→第二根回測→第三根進場。",
            "重大數據公布前停手觀察，等結構重建後再進場，盤整區間內不做。",
            "停利策略：M15 區間 1:1 爬樓梯，TP1 達到後停損移至保本，M15 兩層收手。"
        ],
        "macro_analysis": f"當前黃金價格 {price:.2f} USD，今日{'上漲' if is_up else '下跌'} {abs(change):.2f} USD。聯準會政策方向持續影響黃金走勢，市場關注通膨數據與就業數據。技術面建議等待 M15 明確突破訊號後進場，停損設於結構外，1:1 爬樓梯停利。"
    }


def generate_market_article(market_data: dict) -> dict:
    """
    【夥伴4新增】Claude Sonnet 生成 250 字市場分析文章
    供 market_articles 表使用
    """
    if not ANTHROPIC_KEY:
        today = datetime.now().strftime("%Y年%m月%d日")
        return {
            "title": f"黃金市場分析 {today}",
            "content": f"黃金現貨報 {market_data['xau_price']} 美元，美元指數 {market_data['dxy_price']}，請密切關注後續走勢。"
        }

    xau = market_data["xau_price"]
    xau_chg = market_data["xau_change_pct"]
    dxy = market_data["dxy_price"]
    dxy_chg = market_data["dxy_change_pct"]
    today = datetime.now().strftime("%Y年%m月%d日")

    xau_dir = "上漲" if xau_chg > 0 else "下跌"
    dxy_dir = "走強" if dxy_chg > 0 else "走弱"

    prompt = f"""你是專業貴金屬市場分析師，請根據以下數據撰寫給台灣投資人的市場分析文章。

【{today} 市場數據】
- 黃金現貨（XAU/USD）：{xau} 美元/盎司，較前收{xau_dir} {abs(xau_chg):.2f}%
- 美元指數（DXY）：{dxy}，較前收{dxy_dir} {abs(dxy_chg):.3f}%

【要求】
- 字數：200～250字，繁體中文
- 結構：行情點評 → 黃金與美元相關性分析 → 短線操作觀點
- 語氣：專業易懂，適合有基礎的散戶投資人
- 最後一句附上一個具體支撐或壓力價位

格式：
標題：[15字以內標題]
正文：
[正文]"""

    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()

        title = ""
        content_lines = []
        in_content = False

        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("標題："):
                title = line.replace("標題：", "").strip()
            elif line.startswith("正文："):
                in_content = True
            elif in_content and line:
                content_lines.append(line)

        if not title:
            title = f"黃金市場分析 {today}"
        content = "\n".join(content_lines) if content_lines else raw

        return {"title": title, "content": content.strip()}

    except Exception as e:
        print(f"generate_market_article 失敗: {e}")
        return {
            "title": f"黃金市場分析 {today}",
            "content": f"黃金現貨報 {xau} 美元（{xau_dir} {abs(xau_chg):.2f}%），美元指數 {dxy}（{dxy_dir} {abs(dxy_chg):.3f}%），請關注後續走勢。"
        }


# ─── 排程任務 ────────────────────────────────────────────────

def run_daily_analysis():
    """原有：每天 06:00 台灣時間執行 daily_analysis"""
    print("開始執行每日自動分析...")
    today = datetime.utcnow().strftime('%Y-%m-%d')

    price, change, change_pct = fetch_gold_price()
    eco_events = fetch_eco_events()
    news = fetch_gold_news()
    analysis = generate_analysis_with_claude(price, change, change_pct, eco_events, news)

    try:
        conn = get_db()
        conn.run("""
            INSERT INTO daily_analysis
                (date, direction, gold_price, price_change, bias_text, direction_text,
                 key_levels, macro_text, eco_events, news_items)
            VALUES
                (:date, :direction, :gold_price, :price_change, :bias_text, :direction_text,
                 :key_levels, :macro_text, :eco_events, :news_items)
            ON CONFLICT (date) DO UPDATE SET
                direction = EXCLUDED.direction,
                gold_price = EXCLUDED.gold_price,
                price_change = EXCLUDED.price_change,
                bias_text = EXCLUDED.bias_text,
                direction_text = EXCLUDED.direction_text,
                macro_text = EXCLUDED.macro_text,
                eco_events = EXCLUDED.eco_events,
                news_items = EXCLUDED.news_items,
                updated_at = CURRENT_TIMESTAMP
        """,
            date=today,
            direction=analysis['direction'],
            gold_price=price,
            price_change=change,
            bias_text=json.dumps(analysis, ensure_ascii=False),
            direction_text=analysis['direction_text'],
            key_levels='{}',
            macro_text=analysis['macro_analysis'],
            eco_events=json.dumps(eco_events, ensure_ascii=False),
            news_items=json.dumps(news, ensure_ascii=False)
        )
        print(f"每日分析已儲存：{today}")
    except Exception as e:
        print(f"run_daily_analysis 儲存失敗: {e}")


def run_partner4_article():
    """
    【夥伴4新增】週一三五 08:00 台灣時間
    抓 XAU + DXY → Claude Sonnet 生成文章 → 存入 market_articles
    """
    print("=== 夥伴4：開始生成市場分析文章 ===")

    market_data = fetch_market_data_yfinance()
    article = generate_market_article(market_data)

    try:
        conn = get_db()
        conn.run("""
            INSERT INTO market_articles
                (title, content, xau_price, xau_change_pct, dxy_price, dxy_change_pct)
            VALUES
                (:title, :content, :xau_price, :xau_change_pct, :dxy_price, :dxy_change_pct)
        """,
            title=article["title"],
            content=article["content"],
            xau_price=market_data["xau_price"],
            xau_change_pct=market_data["xau_change_pct"],
            dxy_price=market_data["dxy_price"],
            dxy_change_pct=market_data["dxy_change_pct"]
        )
        print(f"夥伴4文章已儲存：{article['title']}")
    except Exception as e:
        print(f"run_partner4_article 儲存失敗: {e}")


def scheduler():
    """
    統一排程器（單一 thread）
    - 每天 22:00 UTC (台灣 06:00)：run_daily_analysis（平日）
    - 每週一三五 00:00 UTC (台灣 08:00)：run_partner4_article
    """
    while True:
        now_utc = datetime.utcnow()
        weekday = now_utc.weekday()  # 0=週一 … 6=週日

        # 計算今天所有待執行時間點
        jobs_today = []

        # 每日分析：22:00 UTC，週一到週五
        if weekday < 5:
            t_daily = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
            if now_utc < t_daily:
                jobs_today.append((t_daily, "daily_analysis"))

        # 夥伴4文章：00:00 UTC（台灣 08:00），週一(0)、三(2)、五(4)
        if weekday in (0, 2, 4):
            t_p4 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            # 如果 00:00 已過，下次是明天同一個週次（會在下一輪迴圈處理）
            if now_utc < t_p4:
                jobs_today.append((t_p4, "partner4"))

        if jobs_today:
            # 找最近的一個任務
            jobs_today.sort(key=lambda x: x[0])
            next_time, job_name = jobs_today[0]
            wait_sec = (next_time - now_utc).total_seconds()
            print(f"下次任務：{job_name} @ {next_time} UTC（等待 {wait_sec/3600:.1f} 小時）")
            time.sleep(max(wait_sec, 1))

            if job_name == "daily_analysis":
                run_daily_analysis()
            elif job_name == "partner4":
                run_partner4_article()
        else:
            # 今天沒有任務了，睡到明天 00:00 UTC
            tomorrow = (now_utc + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_sec = (tomorrow - now_utc).total_seconds()
            print(f"今日無待執行任務，等待至明天 {tomorrow} UTC")
            time.sleep(max(wait_sec, 60))


# 啟動排程 thread
scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()


# ─── API 路由 ────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/health')
def health():
    return jsonify({"status": "running"})


# Claude 連線測試
@app.route('/api/test-claude')
def test_claude():
    if not ANTHROPIC_KEY:
        return jsonify({"ok": False, "error": "找不到 ANTHROPIC_API_KEY 環境變數"}), 500
    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": "請用繁體中文回一句話：Claude 連線測試成功"}]
        )
        return jsonify({"ok": True, "reply": msg.content[0].text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# 手動觸發 daily_analysis
@app.route('/api/generate_analysis', methods=['POST'])
def generate_analysis():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok": False, "error": "未授權"}), 401
    try:
        run_daily_analysis()
        return jsonify({"ok": True, "message": "分析已生成並儲存"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# 取得今日或指定日期分析
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
            return jsonify({
                "ok": True, "live": True, "date": date,
                "gold_price": price, "price_change": change,
                "direction": analysis['direction'],
                "direction_text": analysis['direction_text'],
                "bias_summary": analysis['bias_summary'],
                "bias_points": analysis['bias_points'],
                "macro_text": analysis['macro_analysis'],
                "eco_events": eco_events, "news_items": news
            })
        cols = ['id','date','direction','gold_price','price_change','bias_text',
                'direction_text','key_levels','macro_text','eco_events','news_items',
                'created_at','updated_at']
        row = dict(zip(cols, rows[0]))
        bias = json.loads(row['bias_text']) if row['bias_text'] else {}
        return jsonify({
            "ok": True, "live": False, "date": str(row['date']),
            "gold_price": row['gold_price'], "price_change": row['price_change'],
            "direction": row['direction'], "direction_text": row['direction_text'],
            "bias_summary": bias.get('bias_summary', ''),
            "bias_points": bias.get('bias_points', []),
            "macro_text": row['macro_text'],
            "eco_events": json.loads(row['eco_events']) if row['eco_events'] else [],
            "news_items": json.loads(row['news_items']) if row['news_items'] else []
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# 取得即時黃金價格
@app.route('/api/gold_price', methods=['GET'])
def gold_price():
    price, change, change_pct = fetch_gold_price()
    return jsonify({"ok": True, "price": price, "change": change, "change_pct": change_pct})


# ─── 【夥伴4】市場分析文章 API ────────────────────────────────

@app.route('/api/articles', methods=['GET'])
def get_articles():
    """取得文章列表（支援分頁）"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    offset = (page - 1) * per_page
    try:
        conn = get_db()
        rows = conn.run("""
            SELECT id, title, content, xau_price, xau_change_pct,
                   dxy_price, dxy_change_pct, published_at
            FROM market_articles
            WHERE is_published = TRUE
            ORDER BY published_at DESC
            LIMIT :limit OFFSET :offset
        """, limit=per_page, offset=offset)

        count_row = conn.run("SELECT COUNT(*) FROM market_articles WHERE is_published = TRUE")
        total = count_row[0][0]

        cols = ['id','title','content','xau_price','xau_change_pct',
                'dxy_price','dxy_change_pct','published_at']
        articles = [dict(zip(cols, r)) for r in rows]
        for a in articles:
            a['published_at'] = str(a['published_at'])

        return jsonify({
            "ok": True,
            "articles": articles,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "current_page": page
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/articles/latest', methods=['GET'])
def get_latest_article():
    """取得最新一篇文章（網站 banner 用）"""
    try:
        conn = get_db()
        rows = conn.run("""
            SELECT id, title, content, xau_price, xau_change_pct,
                   dxy_price, dxy_change_pct, published_at
            FROM market_articles
            WHERE is_published = TRUE
            ORDER BY published_at DESC
            LIMIT 1
        """)
        if not rows:
            return jsonify({"ok": False, "error": "尚無文章"}), 404
        cols = ['id','title','content','xau_price','xau_change_pct',
                'dxy_price','dxy_change_pct','published_at']
        article = dict(zip(cols, rows[0]))
        article['published_at'] = str(article['published_at'])
        return jsonify({"ok": True, "article": article})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/articles/trigger', methods=['POST'])
def trigger_partner4():
    """手動觸發夥伴4（測試用）"""
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok": False, "error": "未授權"}), 401
    try:
        run_partner4_article()
        conn = get_db()
        rows = conn.run("""
            SELECT id, title, published_at FROM market_articles
            ORDER BY published_at DESC LIMIT 1
        """)
        latest = {"id": rows[0][0], "title": rows[0][1], "published_at": str(rows[0][2])} if rows else None
        return jsonify({"ok": True, "message": "夥伴4任務執行完成", "article": latest})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── 申請相關 API（原有，不動）────────────────────────────────

@app.route('/api/apply', methods=['POST'])
def apply():
    data = request.json
    name = data.get('name', '').strip()
    telegram = data.get('telegram', '').strip()
    email = data.get('email', '').strip()
    experience = data.get('experience', '').strip()
    reason = data.get('reason', '').strip()
    if not name or not telegram or not email:
        return jsonify({"ok": False, "error": "請填入所有必填欄位"}), 400
    try:
        conn = get_db()
        conn.run(
            "INSERT INTO applications (name, telegram, email, experience, reason) "
            "VALUES (:name, :telegram, :email, :experience, :reason)",
            name=name, telegram=telegram, email=email,
            experience=experience, reason=reason
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/applications', methods=['GET'])
def get_applications():
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok": False, "error": "未授權"}), 401
    try:
        conn = get_db()
        rows = conn.run("SELECT * FROM applications ORDER BY created_at DESC")
        cols = ['id','name','telegram','email','experience','reason','status','created_at']
        return jsonify({"ok": True, "data": [dict(zip(cols, r)) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/applications/<int:app_id>', methods=['PATCH'])
def update_application(app_id):
    if request.headers.get('X-Admin-Key') != ADMIN_KEY:
        return jsonify({"ok": False, "error": "未授權"}), 401
    status = request.json.get('status')
    if status not in ['approved', 'rejected', 'pending']:
        return jsonify({"ok": False, "error": "無效狀態"}), 400
    try:
        conn = get_db()
        conn.run("UPDATE applications SET status = :status WHERE id = :id",
                 status=status, id=app_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/<path:path>')
def serve_file(path):
    return send_from_directory('.', path)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
