"""
Orchestrator — 每日自動化市場分析流水線
流程：抓取市場數據 → 夥伴4（市場分析文章）→ 夥伴3（Threads 草稿）→ 存入 PostgreSQL
排程：台灣時間 08:00（UTC 00:00），透過 APScheduler 執行
"""
import os
import json
import urllib.parse
from datetime import datetime

import requests
import pg8000.native
import yfinance as yf
from openai import OpenAI

# ─── DB 連線 ───────────────────────────────────────────────

def _get_db():
    url = urllib.parse.urlparse(os.environ.get("DATABASE_URL"))
    return pg8000.native.Connection(
        user=url.username, password=url.password,
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:]
    )

# ─── 建表（初始化時呼叫） ─────────────────────────────────

def init_orchestrator_tables():
    """建立 Orchestrator 專用的兩張表，如果已存在則跳過"""
    conn = _get_db()
    # 夥伴4 產出：結構化市場分析文章（每日一篇）
    conn.run("""CREATE TABLE IF NOT EXISTS orchestrator_articles (
        id SERIAL PRIMARY KEY,
        date DATE NOT NULL,
        title VARCHAR(200),
        market_status TEXT,
        key_levels TEXT,
        gino_strategy TEXT,
        weekly_notes TEXT,
        full_content TEXT,
        xau_price FLOAT,
        dxy_price FLOAT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # 夥伴3 產出：Threads 貼文草稿（每日3個版本）
    conn.run("""CREATE TABLE IF NOT EXISTS social_drafts (
        id SERIAL PRIMARY KEY,
        date DATE NOT NULL,
        version INTEGER NOT NULL,
        platform VARCHAR(50) DEFAULT 'threads',
        style VARCHAR(100),
        content TEXT NOT NULL,
        is_published BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    print("Orchestrator 資料表初始化完成")

# ─── 夥伴4 系統提示詞 ─────────────────────────────────────

PARTNER4_SYSTEM = """你是「金融內容整合編輯」，擁有10年以上財經媒體、金融研究與內容行銷工作經驗。你的專長是將複雜的市場數據轉化成清晰、易懂且具有實用價值的金融內容。

你的任務是每天根據最新市場數據，生成一篇精簡、專業但易讀的黃金市場分析文章，供外匯講師 Gino 使用。

【輸出格式】只回傳純 JSON，不要任何 markdown 符號或說明文字：
{
  "title": "標題（15字以內，吸引人）",
  "market_status": "今日行情摘要（50字以內）",
  "key_levels": "關鍵價位，格式：支撐 XXXX / 壓力 XXXX（可多個，用 / 分隔）",
  "gino_strategy": "Gino操盤方向與策略（80字以內，口語化，像老師在跟學員說話）",
  "weekly_notes": "本週重要觀察事項（50字以內）",
  "full_content": "完整文章（250字以內，繁體中文）"
}

風格要求：
- 專業但口語化，像 Gino 老師在跟學員講話
- 不用艱深術語，讓有基礎的投資人都能理解
- 數字直接標出（不用「約」「左右」等模糊詞）
- 以教育分享為主，不構成投資建議"""

# ─── 夥伴3 系統提示詞 ─────────────────────────────────────

PARTNER3_SYSTEM = """你是「社群內容創作夥伴」，擁有10年以上社群行銷、品牌內容與數位媒體工作經驗，熟悉台灣 Instagram、Threads 平台生態。

【人物基本資料】
Gino（朱育生），Threads 帳號：yusheng.zhu.14
黃金交易員＋交易教學老師，交易資歷2.5年，主要商品 XAUUSD，日內單。
已婚8年兩個小孩，從餐飲業便當店轉型。
每週二四晚上 21:00 群內直播。

【故事線背景】
從餐飲業跳脫，某夜賠掉5000美金反而解脫，因為知道錯在哪。
跟著老師調整後每月穩定出金，現在帶學員一起學交易。

【目標受眾】
用時間換錢想改變的人，25-40歲，有家庭責任。

【貼文風格（嚴格模仿以下範例語氣）】
1. 溫馨提醒型：
「溫馨提醒：今天黃金在4520附近晃，上面先關注4535、4547。今晚沒重大數據，別硬做！各位還活著嗎🤣」

2. 市場回顧型：
「早上做黃金真的被洗到差點中風🥲 行情來回刷，先試空被掃損，中午反手多單，4498上車，最後停利4531，各位有跟到的留言讓我知道😊」

3. 個人觀點型：
「明天黃金先看戲，觀察有沒有反彈到4337、4366 再決定做多做空🙃」

4. 生活混搭型：
「五月最後一天完美關門，感謝老川畫線😍 黃金繼續關注35、47、60，我要騎車了你們自己盯🤣」

5. 互動幽默型：
「台股跌了買一點，大跌再買一點 合理的買入都是為了以後不用睡公園 各位認同嗎🤣🤣🤣」

【語氣規則】
- 口語直接、有溫度、真實感、不過度勵志
- 短句為主，一句一行，閱讀節奏快
- 重要數字直接標出，例如：4337、4366、99.5
- 稱呼讀者：「各位」「脆友們」「兄弟姊妹們」
- 適度 emoji（🤣😊😍🥲👍🙃），不過度使用
- 偶爾互動結尾問問題
- 招牌開頭「溫馨提醒：」用於市場提示類
- 不說教、不保證獲利、不用艱深術語

【產出規則】
- Threads 脆貼文：50～150 字
- 每次輸出 3 個不同角度版本
- 以教育分享為主，不構成投資建議

【輸出格式】只回傳純 JSON，不要任何 markdown 符號或說明文字：
{
  "drafts": [
    {"version": 1, "style": "溫馨提醒型", "content": "..."},
    {"version": 2, "style": "市場回顧型", "content": "..."},
    {"version": 3, "style": "個人觀點型", "content": "..."}
  ]
}"""

# ─── 市場數據抓取 ─────────────────────────────────────────

def _fetch_market_data():
    """XAU/USD 從 Twelve Data 抓取；DXY 從 yfinance（Twelve Data 不支援此 symbol）"""
    td_key = os.environ.get("TWELVE_DATA_KEY")

    # ── XAU/USD via Twelve Data ──
    xau_price, xau_chg = 0.0, 0.0
    if not td_key:
        print("[Orchestrator] TWELVE_DATA_KEY 未設定，XAU/USD 將為 0")
    else:
        try:
            r = requests.get(
                f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={td_key}",
                timeout=10
            )
            d = r.json()
            if d.get("status") == "error" or d.get("code"):
                print(f"[Orchestrator] Twelve Data XAU/USD 錯誤: {d}")
            else:
                xau_price = round(float(d.get("close") or 0), 2)
                xau_chg = round(float(d.get("percent_change") or 0), 2)
                if xau_price == 0:
                    print(f"[Orchestrator] Twelve Data XAU/USD 回傳 0，完整回應: {d}")
        except Exception as e:
            print(f"[Orchestrator] Twelve Data XAU/USD 例外: {e}")

    # ── DXY via yfinance（Twelve Data 無此品種）──
    dxy_price, dxy_chg = 0.0, 0.0
    try:
        di = yf.Ticker("DX-Y.NYB").fast_info
        dxy_price = round(di.last_price, 3)
        dxy_chg = round(((di.last_price - di.previous_close) / di.previous_close) * 100, 3)
    except Exception as e:
        print(f"[Orchestrator] yfinance DXY 例外: {e}")

    return {
        "xau_price": xau_price,
        "xau_change_pct": xau_chg,
        "dxy_price": dxy_price,
        "dxy_change_pct": dxy_chg,
    }

# ─── 夥伴4：生成市場分析文章 ─────────────────────────────

def _run_partner4(market_data):
    """用夥伴4 system prompt + 當日數據，生成結構化市場分析 JSON"""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        return None

    xau = market_data["xau_price"]
    xau_chg = market_data["xau_change_pct"]
    dxy = market_data["dxy_price"]
    dxy_chg = market_data["dxy_change_pct"]
    today = datetime.now().strftime("%Y年%m月%d日")
    direction = "上漲" if xau_chg > 0 else "下跌"
    dxy_direction = "走強" if dxy_chg > 0 else "走弱"

    user_prompt = f"""【{today} 市場數據】
黃金現貨（XAU/USD）：{xau} USD（今日{direction} {abs(xau_chg):.2f}%）
美元指數（DXY）：{dxy}（今日{dxy_direction} {abs(dxy_chg):.3f}%）

請根據以上數據生成今日黃金市場分析，輸出格式嚴格遵照系統指示的 JSON 結構。"""

    try:
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=800,
            messages=[
                {"role": "system", "content": PARTNER4_SYSTEM},
                {"role": "user", "content": user_prompt}
            ]
        )
        raw = resp.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[Orchestrator] 夥伴4文章生成失敗: {e}")
        return None

# ─── 夥伴3：根據夥伴4文章生成 Threads 草稿 ───────────────

def _run_partner3(article, market_data):
    """把夥伴4的分析文章傳給夥伴3，產出3個版本的 Threads 草稿"""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key or not article:
        return []

    xau = market_data["xau_price"]
    xau_chg = market_data["xau_change_pct"]
    direction = "上漲" if xau_chg > 0 else "下跌"

    # 把夥伴4的分析整理成給夥伴3的素材
    article_summary = f"""【今日黃金分析素材】
標題：{article.get('title', '')}
行情摘要：{article.get('market_status', '')}
關鍵價位：{article.get('key_levels', '')}
操盤方向：{article.get('gino_strategy', '')}
本週重點：{article.get('weekly_notes', '')}

今日 XAU/USD：{xau} USD（{direction} {abs(xau_chg):.2f}%）"""

    user_prompt = f"""以下是今日的市場分析素材，請根據這些內容為 Gino 生成3個版本的 Threads 貼文草稿：

{article_summary}

嚴格按照系統指示的 JSON 格式輸出，3個版本分別用不同貼文類型。"""

    try:
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=1200,
            messages=[
                {"role": "system", "content": PARTNER3_SYSTEM},
                {"role": "user", "content": user_prompt}
            ]
        )
        raw = resp.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return result.get("drafts", [])
    except Exception as e:
        print(f"[Orchestrator] 夥伴3草稿生成失敗: {e}")
        return []

# ─── 儲存到 DB ────────────────────────────────────────────

def _save_article(conn, date_str, article, market_data):
    """夥伴4文章存入 orchestrator_articles"""
    conn.run(
        """INSERT INTO orchestrator_articles
            (date, title, market_status, key_levels, gino_strategy, weekly_notes, full_content, xau_price, dxy_price)
           VALUES (:date, :title, :market_status, :key_levels, :gino_strategy, :weekly_notes, :full_content, :xau_price, :dxy_price)""",
        date=date_str,
        title=article.get("title", ""),
        market_status=article.get("market_status", ""),
        key_levels=article.get("key_levels", ""),
        gino_strategy=article.get("gino_strategy", ""),
        weekly_notes=article.get("weekly_notes", ""),
        full_content=article.get("full_content", ""),
        xau_price=market_data["xau_price"],
        dxy_price=market_data["dxy_price"]
    )

def _save_social_drafts(conn, date_str, drafts):
    """夥伴3草稿存入 social_drafts"""
    for d in drafts:
        conn.run(
            """INSERT INTO social_drafts (date, version, platform, style, content)
               VALUES (:date, :version, :platform, :style, :content)""",
            date=date_str,
            version=d.get("version", 0),
            platform="threads",
            style=d.get("style", ""),
            content=d.get("content", "")
        )

# ─── Telegram 推送 ────────────────────────────────────────

def _tg_send(text):
    """發送單則 Telegram 訊息，使用 HTML 格式"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Orchestrator] Telegram 環境變數未設定，跳過推送")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        if not resp.ok:
            print(f"[Orchestrator] Telegram 推送失敗: {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"[Orchestrator] Telegram 推送例外: {e}")
        return False

def _push_to_telegram(article, drafts):
    """依序推送夥伴4文章 + 夥伴3三個草稿到 Telegram"""
    today = datetime.now().strftime("%Y年%m月%d日")

    # ── 訊息1：夥伴4 市場分析 ──────────────────────────────
    msg1 = (
        f"📊 <b>今日黃金市場分析</b>  {today}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>{article.get('title','')}</b>\n\n"
        f"📈 <b>行情摘要</b>\n{article.get('market_status','')}\n\n"
        f"🎯 <b>關鍵價位</b>\n{article.get('key_levels','')}\n\n"
        f"⚡ <b>操盤方向</b>\n{article.get('gino_strategy','')}\n\n"
        f"📅 <b>本週重點</b>\n{article.get('weekly_notes','')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{article.get('full_content','')}"
    )
    _tg_send(msg1)

    # ── 訊息2-4：夥伴3 各版本草稿（分開發送方便複製）──────
    version_labels = {1: "V1 溫馨提醒型", 2: "V2 市場回顧型", 3: "V3 個人觀點型"}
    for d in drafts:
        ver = d.get("version", 0)
        style = d.get("style") or version_labels.get(ver, f"V{ver}")
        msg = (
            f"✍️ <b>{style}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{d.get('content','')}"
        )
        _tg_send(msg)

    print(f"[Orchestrator] Telegram 推送完成：1篇文章 + {len(drafts)} 個草稿")

# ─── 主流程 ───────────────────────────────────────────────

def run_orchestrator():
    """
    Orchestrator 主流程：
    1. 抓取 XAU/USD、DXY 數據
    2. 夥伴4 生成市場分析文章
    3. 夥伴3 根據夥伴4文章生成3個 Threads 草稿
    4. 存入 PostgreSQL
    """
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[Orchestrator] 開始執行 {today}")

    # 步驟1：抓市場數據
    market_data = _fetch_market_data()
    print(f"[Orchestrator] 市場數據：XAU={market_data['xau_price']} DXY={market_data['dxy_price']}")

    # 步驟2：夥伴4 生成分析文章
    article = _run_partner4(market_data)
    if not article:
        print("[Orchestrator] 夥伴4失敗，中止流程")
        return

    print(f"[Orchestrator] 夥伴4完成：{article.get('title', '')}")

    # 步驟3：夥伴3 根據夥伴4文章生成 Threads 草稿
    drafts = _run_partner3(article, market_data)
    print(f"[Orchestrator] 夥伴3完成：{len(drafts)} 個草稿")

    # 步驟4：存入 DB
    try:
        conn = _get_db()
        _save_article(conn, today, article, market_data)
        if drafts:
            _save_social_drafts(conn, today, drafts)
        print(f"[Orchestrator] 儲存完成：{today}")
    except Exception as e:
        print(f"[Orchestrator] 儲存失敗: {e}")

    # 步驟5：推送到 Telegram
    _push_to_telegram(article, drafts)
