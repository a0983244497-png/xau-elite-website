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

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "xauelite2024")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "d8ie1s9r01qm63bbl4qgd8ie1s9r01qm63bbl4r0")
TZ_OFFSET = 8  # 台灣時間 UTC+8

def get_db():
    url = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        user=url.username, password=url.password,
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:]
    )

def init_db():
    conn = get_db()
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

try:
    init_db()
    print("資料庫初始化成功")
except Exception as e:
    print(f"資料庫初始化失敗: {e}")

# ─── Finnhub 數據抓取 ───────────────────────────────────────

def fetch_gold_price():
    try:
        res = requests.get(f"https://finnhub.io/api/v1/quote?symbol=OANDA:XAU_USD&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        return data.get('c', 0), data.get('d', 0), data.get('dp', 0)
    except:
        return 0, 0, 0

def fetch_eco_events():
    try:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        res = requests.get(f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={today}&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        events = [e for e in (data.get('economicCalendar') or [])
                  if e.get('country') in ['US', 'USD']][:5]
        return events
    except:
        return []

def fetch_gold_news():
    try:
        res = requests.get(f"https://finnhub.io/api/v1/news?category=forex&token={FINNHUB_KEY}", timeout=10)
        data = res.json()
        news = [n for n in data if n.get('headline') and any(
            kw in n['headline'].lower() for kw in ['gold','xau','fed','dollar','inflation']
        )][:5]
        return news
    except:
        return []

# ─── Claude API 生成分析 ────────────────────────────────────

def generate_analysis_with_claude(price, change, change_pct, eco_events, news):
    if not ANTHROPIC_KEY:
        return generate_fallback_analysis(price, change)

    eco_text = "\n".join([f"- {e.get('event','')}: 實際 {e.get('actual','—')} 預期 {e.get('estimate','—')} 前值 {e.get('prev','—')}" for e in eco_events]) or "今日無重大數據"
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

請生成以下四個部分的分析，格式為 JSON：

{{
  "direction": "bullish" 或 "bearish" 或 "neutral",
  "direction_text": "多方偏向 BULLISH" 或 "空方偏向 BEARISH" 或 "多空觀望 NEUTRAL",
  "bias_summary": "一句話說明今日偏向（約50字）",
  "bias_points": [
    "重點1（約30字，說明整體方向）",
    "重點2（約30字，說明M15進場條件）",
    "重點3（約30字，說明風險注意事項）",
    "重點4（約30字，說明停利策略）"
  ],
  "macro_analysis": "總經背景分析（約150字，包含Fed政策、美元走勢、技術面總結）"
}}

注意：
- 只回傳 JSON，不要其他文字
- 繁體中文
- 分析要具體且專業
- 技術面結合破框 SOP 邏輯（突破→回測→第三根進場）"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        data = res.json()
        text = data['content'][0]['text']
        text = text.replace('```json','').replace('```','').strip()
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

# ─── 自動生成並儲存每日分析 ─────────────────────────────────

def run_daily_analysis():
    print("開始執行每日自動分析...")
    today = datetime.utcnow().strftime('%Y-%m-%d')

    # 抓數據
    price, change, change_pct = fetch_gold_price()
    eco_events = fetch_eco_events()
    news = fetch_gold_news()

    # Claude 生成分析
    analysis = generate_analysis_with_claude(price, change, change_pct, eco_events, news)

    # 存進資料庫
    try:
        conn = get_db()
        conn.run("""
            INSERT INTO daily_analysis (date, direction, gold_price, price_change, bias_text, direction_text, key_levels, macro_text, eco_events, news_items)
            VALUES (:date, :direction, :gold_price, :price_change, :bias_text, :direction_text, :key_levels, :macro_text, :eco_events, :news_items)
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
        news_items=json.dumps(news, ensure_ascii=False))
        print(f"每日分析已儲存：{today}")
    except Exception as e:
        print(f"儲存失敗: {e}")

# ─── 定時任務（每天 06:00 台灣時間 = 22:00 UTC）──────────────

def scheduler():
    while True:
        now_utc = datetime.utcnow()
        target_hour = 22  # UTC 22:00 = 台灣 06:00
        next_run = now_utc.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if now_utc >= next_run:
            next_run += timedelta(days=1)
        wait_sec = (next_run - now_utc).total_seconds()
        print(f"下次自動分析：{next_run} UTC（等待 {wait_sec/3600:.1f} 小時）")
        time.sleep(wait_sec)
        # 週六日跳過（黃金休市）
        weekday = datetime.utcnow().weekday()
        if weekday < 5:
            run_daily_analysis()
        else:
            print("週末休市，跳過自動分析")

# 啟動排程
scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()

# ─── API 路由 ────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:path>')
def serve_file(path):
    return send_from_directory('.', path)

# 手動觸發生成分析（後台用）
@app.route('/api/generate_analysis', methods=['POST'])
def generate_analysis():
    admin_key = request.headers.get('X-Admin-Key')
    if admin_key != ADMIN_KEY:
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
            # 即時生成
            price, change, change_pct = fetch_gold_price()
            eco_events = fetch_eco_events()
            news = fetch_gold_news()
            analysis = generate_analysis_with_claude(price, change, change_pct, eco_events, news)
            return jsonify({
                "ok": True,
                "live": True,
                "date": date,
                "gold_price": price,
                "price_change": change,
                "direction": analysis['direction'],
                "direction_text": analysis['direction_text'],
                "bias_summary": analysis['bias_summary'],
                "bias_points": analysis['bias_points'],
                "macro_text": analysis['macro_analysis'],
                "eco_events": eco_events,
                "news_items": news
            })

        cols = ['id','date','direction','gold_price','price_change','bias_text','direction_text','key_levels','macro_text','eco_events','news_items','created_at','updated_at']
        row = dict(zip(cols, rows[0]))
        bias = json.loads(row['bias_text']) if row['bias_text'] else {}
        return jsonify({
            "ok": True,
            "live": False,
            "date": str(row['date']),
            "gold_price": row['gold_price'],
            "price_change": row['price_change'],
            "direction": row['direction'],
            "direction_text": row['direction_text'],
            "bias_summary": bias.get('bias_summary',''),
            "bias_points": bias.get('bias_points',[]),
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

# 申請相關 API
@app.route('/api/apply', methods=['POST'])
def apply():
    data = request.json
    name = data.get('name','').strip()
    telegram = data.get('telegram','').strip()
    email = data.get('email','').strip()
    experience = data.get('experience','').strip()
    reason = data.get('reason','').strip()
    if not name or not telegram or not email:
        return jsonify({"ok": False, "error": "請填入所有必填欄位"}), 400
    try:
        conn = get_db()
        conn.run("INSERT INTO applications (name, telegram, email, experience, reason) VALUES (:name, :telegram, :email, :experience, :reason)",
                 name=name, telegram=telegram, email=email, experience=experience, reason=reason)
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
    if status not in ['approved','rejected','pending']:
        return jsonify({"ok": False, "error": "無效狀態"}), 400
    try:
        conn = get_db()
        conn.run("UPDATE applications SET status = :status WHERE id = :id", status=status, id=app_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({"status": "running"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
