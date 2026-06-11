"""
Telegram Bot Webhook 處理器
接收 gino_content_bot 的訊息，根據關鍵字觸發對應 AI 流程並回傳結果
"""
import os
import threading
import requests as http
from datetime import datetime
import yfinance as yf
from openai import OpenAI

# ─── 基礎工具 ──────────────────────────────────────────────

def _send(chat_id, text):
    """發送 HTML 格式訊息到指定 chat"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
    except Exception as e:
        print(f"[TG Bot] 發送失敗: {e}")

def _gpt(system, user, max_tokens=800):
    """呼叫 GPT-4o，回傳純文字"""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return "（OpenAI API Key 未設定）"
    try:
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[TG Bot] GPT 呼叫失敗: {e}")
        return f"（AI 生成失敗：{e}）"

def _market():
    """取得 XAU/USD 與 DXY 當前數據，回傳 dict"""
    try:
        xi = yf.Ticker("GC=F").fast_info
        di = yf.Ticker("DX-Y.NYB").fast_info
        xau_p = round(xi.last_price, 2)
        xau_c = round(((xi.last_price - xi.previous_close) / xi.previous_close) * 100, 2)
        dxy_p = round(di.last_price, 3)
        dxy_c = round(((di.last_price - di.previous_close) / di.previous_close) * 100, 3)
        return {"xau": xau_p, "xau_chg": xau_c, "dxy": dxy_p, "dxy_chg": dxy_c}
    except Exception as e:
        print(f"[TG Bot] 市場數據失敗: {e}")
        return {"xau": 0, "xau_chg": 0, "dxy": 0, "dxy_chg": 0}

def _news_text(limit=10):
    """從 Finnhub 抓取財經新聞，回傳純文字清單"""
    fkey = os.environ.get("FINNHUB_KEY")
    if not fkey:
        return "（FINNHUB_KEY 未設定）"
    kws = ['gold','xau','fed','dollar','inflation','rate','treasury','yield','fomc','gdp','cpi','market']
    all_items, seen, result = [], set(), []
    for cat in ['general', 'forex']:
        try:
            r = http.get(f"https://finnhub.io/api/v1/news?category={cat}&token={fkey}", timeout=10)
            if isinstance(r.json(), list):
                all_items.extend(r.json())
        except:
            pass
    for n in all_items:
        nid = n.get('id') or n.get('url', '')
        if nid in seen:
            continue
        seen.add(nid)
        hl = n.get('headline', '')
        if hl and any(kw in hl.lower() for kw in kws):
            result.append(hl)
        if len(result) >= limit:
            break
    return "\n".join(f"- {h}" for h in result) if result else "（今日暫無相關新聞）"

def _today():
    return datetime.now().strftime("%Y年%m月%d日")

# ─── System Prompts ────────────────────────────────────────

_P4 = """你是「金融內容整合編輯」，10年以上財經媒體與內容行銷經驗。
服務品牌：外匯講師 Gino（XAU Elite 黃金交易訊號）。
輸出：繁體中文、口語、專業易懂。不構成投資建議。"""

_P3 = """你是「社群內容創作夥伴」，熟悉台灣 Threads、Instagram 生態。
服務品牌：外匯講師 Gino（Threads：yusheng.zhu.14）。
風格：朋友聊天、短句一行、數字直接標、結尾問句互動、emoji 自然帶入。
稱呼：各位 / 脆友們 / 兄弟姊妹們。不構成投資建議。輸出：繁體中文。"""

_P1 = """你是「外匯教學夥伴」，負責為 Gino 老師撰寫外匯與黃金交易的基礎教學內容。
目標：台灣有基礎的投資人。風格：清楚易懂、口語、重點條列、適合社群分享。
不構成投資建議。輸出：繁體中文。"""

# ─── 指令 Handler ──────────────────────────────────────────

def cmd_daily_market(chat_id):
    """今日市場：夥伴4 完整分析 + 夥伴3 三個草稿"""
    m = _market()
    d = _today()
    xdir = "上漲" if m['xau_chg'] > 0 else "下跌"
    ddir = "走強" if m['dxy_chg'] > 0 else "走弱"

    article = _gpt(_P4, f"""【{d} 市場數據】
黃金：{m['xau']} USD（{xdir} {abs(m['xau_chg']):.2f}%）
美元指數：{m['dxy']}（{ddir} {abs(m['dxy_chg']):.3f}%）

產出今日市場分析，格式：
標題：（15字內）
行情摘要：（50字）
關鍵位：（支撐 XXXX / 壓力 XXXX）
操盤方向：（80字口語）
完整文章：（250字以內）""", max_tokens=900)

    _send(chat_id, f"📊 <b>今日市場分析</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{article}")

    drafts = _gpt(_P3, f"""今日黃金 {m['xau']} USD（{xdir} {abs(m['xau_chg']):.2f}%），DXY {m['dxy']}。
為 Gino 生成3個 Threads 草稿，每個50~150字，分別標示：
V1 溫馨提醒型 / V2 市場回顧型 / V3 個人觀點型""", max_tokens=1000)

    _send(chat_id, f"✍️ <b>今日 Threads 草稿</b>\n━━━━━━━━━━━━━━━━━━━━\n\n{drafts}")


def cmd_strategy(chat_id):
    """策略：H4/H1/M15 多週期操作方向"""
    m = _market()
    d = _today()
    xdir = "上漲" if m['xau_chg'] > 0 else "下跌"

    result = _gpt(_P4, f"""【{d}】黃金 {m['xau']} USD（{xdir} {abs(m['xau_chg']):.2f}%），DXY {m['dxy']}

用 H4/H1/M15 多週期框架產出今日操作策略：
- H4：大方向（多/空/中性）
- H1：關鍵結構觀察
- M15：進場觸發條件
- 關鍵支撐壓力位
- 今日建議（不做/輕做/正常做 + 理由）
口語，像老師跟學員說話，250字以內。""", max_tokens=600)

    _send(chat_id, f"🎯 <b>今日操作策略</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_key_focus(chat_id):
    """今日重點關注：市場重點 + 風險提示"""
    m = _market()
    d = _today()

    result = _gpt(_P4, f"""【{d}】XAU/USD {m['xau']} USD，DXY {m['dxy']}

產出今日重點關注，格式：
🔍 重點關注（3~5點，每點一行）
⚠️ 風險提示（2~3點）
📌 關鍵價位（支撐 / 壓力）
條列清楚，200字以內。""", max_tokens=500)

    _send(chat_id, f"🔍 <b>今日重點關注</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def _google_trends_taiwan():
    """抓取 Google Trends 台灣今日熱搜話題（RSS，免 API key）"""
    try:
        import xml.etree.ElementTree as ET
        r = http.get(
            "https://trends.google.com/trending/rss?geo=TW",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        root = ET.fromstring(r.text)
        topics = []
        for item in root.findall("./channel/item")[:10]:
            title = item.findtext("title") or ""
            if title.strip():
                topics.append(title.strip())
        return topics
    except Exception as e:
        print(f"[TG Bot] Google Trends 抓取失敗: {e}")
        return []


def _ptt_hot_titles():
    """抓取 PTT 八卦板熱門文章標題"""
    try:
        session = http.Session()
        session.cookies.set("over18", "1", domain="www.ptt.cc")
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        r = session.get("https://www.ptt.cc/bbs/Gossiping/index.json", timeout=10, headers=headers)
        data = r.json()
        titles = []
        for post in data.get("items", [])[:25]:
            title = (post.get("title") or "").strip()
            if title and not title.startswith("Re:") and not title.startswith("Fw:"):
                titles.append(title)
            if len(titles) >= 10:
                break
        return titles
    except Exception as e:
        print(f"[TG Bot] PTT 抓取失敗: {e}")
        return []


def cmd_fun_post(chat_id):
    """今日趣事：Google Trends + PTT 熱門話題 → 夥伴3 幽默互動貼文"""
    trends = _google_trends_taiwan()
    ptt = _ptt_hot_titles()

    trends_text = "\n".join(f"- {t}" for t in trends) if trends else "（無法取得）"
    ptt_text = "\n".join(f"- {t}" for t in ptt) if ptt else "（無法取得）"

    result = _gpt(_P3, f"""以下是今日台灣熱門話題：

【Google Trends 台灣熱搜】
{trends_text}

【PTT 八卦板熱門標題】
{ptt_text}

請從上面選 1~2 個最有趣、最有共鳴的話題，為外匯講師 Gino 寫一篇 Threads「互動幽默型」貼文：
- 話題本身就是主角，不需要硬扯到外匯或金融
- 口語自然、一句一行、帶生活感和笑點
- 稱呼讀者「各位」「脆友們」
- 結尾用問句帶互動（例如「你怎麼看？」「你遇過嗎？」）
- 50~150字""", max_tokens=400)

    _send(chat_id, f"😎 <b>今日趣事貼文</b>\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_big_news(chat_id):
    """今日大新聞：Finnhub 新聞 → 夥伴4 整理摘要"""
    d = _today()
    news = _news_text(10)

    result = _gpt(_P4, f"""【{d} 今日財經新聞】
{news}

整理成重點摘要：
📰 今日財經重點（3~5條，每條一行）
💡 對黃金/外匯市場的影響（100字以內）
口語，條列清楚。""", max_tokens=600)

    _send(chat_id, f"📰 <b>今日財經大新聞</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_weekly(chat_id):
    """週報：夥伴4 本週市場總結"""
    m = _market()
    d = _today()

    result = _gpt(_P4, f"""【{d}】黃金 {m['xau']} USD，DXY {m['dxy']}

產出本週市場週報：
📊 本週行情回顧（50字）
🔑 關鍵事件影響（2~3點）
👀 下週重點觀察（2~3點）
📌 下週操作方向建議
口語，像老師幫學員做週末功課，300字以內。""", max_tokens=700)

    _send(chat_id, f"📅 <b>本週市場週報</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_education(chat_id):
    """教學貼文：夥伴1 外匯/黃金基礎教學"""
    d = _today()

    result = _gpt(_P1, f"""【{d}】隨機選一個外匯或黃金交易的基礎觀念，寫教學貼文。
主題例：支撐壓力、趨勢判斷、風報比、停損設置、時段特性、技術形態...（自行挑一個）

格式：
📚 教學主題：（標題）
內容：（150~200字，淺顯易懂，舉黃金實例）
💡 Gino 提醒：（一句話重點）
結尾問讀者互動。""", max_tokens=600)

    _send(chat_id, f"📚 <b>今日交易教學</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_quote(chat_id):
    """名言：交易相關金句 + Gino 口吻解讀"""
    result = _gpt(
        "你是交易智慧分享者，輸出繁體中文，語氣口語有溫度。",
        "產出一句深刻的交易/投資金句，格式：「金句」—來源\n"
        "然後用 Gino 老師的口吻，2~3句話說明這句話對黃金交易的啟示。",
        max_tokens=300
    )
    _send(chat_id, f"💬 <b>今日交易名言</b>\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_nfp(chat_id):
    """非農：NFP 公布前注意事項與策略提醒"""
    m = _market()
    d = _today()

    result = _gpt(_P4, f"""【{d}】黃金 {m['xau']} USD，DXY {m['dxy']}

產出「非農 NFP 公布前」操作提醒：
- 非農為什麼重要（40字）
- 公布前黃金通常如何波動（50字）
- 今日操作建議（進場時機、停損、要不要做）
- ⚠️ 風險提示（2點）
口語，像老師提醒學員，250字以內。""", max_tokens=600)

    _send(chat_id, f"📊 <b>非農 NFP 前操作提醒</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_fed(chat_id):
    """Fed：聯準會決策市場解讀"""
    m = _market()
    d = _today()

    result = _gpt(_P4, f"""【{d}】黃金 {m['xau']} USD，DXY {m['dxy']}

產出「聯準會 Fed 決策」市場解讀：
- 當前 Fed 政策方向（40字）
- 升息/降息/不動對黃金各自意味什麼
- 目前市場預期與黃金走勢關係
- 操作建議
口語，250字以內。""", max_tokens=600)

    _send(chat_id, f"🏦 <b>Fed 市場解讀</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_risk(chat_id):
    """風險提示：當日重大風險事件提醒"""
    m = _market()
    news = _news_text(6)
    d = _today()

    result = _gpt(_P4, f"""【{d}】黃金 {m['xau']} USD，DXY {m['dxy']}
今日新聞：
{news}

產出今日重大風險提示：
⚠️ 風險事件（3~5點，每點一行）
🛡️ 應對建議（2~3點）
今日適合交易嗎？（直接說是/否 + 理由）
200字以內。""", max_tokens=500)

    _send(chat_id, f"⚠️ <b>今日風險提示</b>  {d}\n━━━━━━━━━━━━━━━━━━━━\n\n{result}")


def cmd_trigger(chat_id):
    """觸發：完整 Orchestrator（同今日市場）"""
    cmd_daily_market(chat_id)


# ─── 指令路由表 ───────────────────────────────────────────

COMMANDS = {
    "今日市場":   cmd_daily_market,
    "策略":       cmd_strategy,
    "今日重點關注": cmd_key_focus,
    "今日趣事":   cmd_fun_post,
    "今日大新聞": cmd_big_news,
    "週報":       cmd_weekly,
    "教學貼文":   cmd_education,
    "名言":       cmd_quote,
    "非農":       cmd_nfp,
    "Fed":        cmd_fed,
    "fed":        cmd_fed,
    "風險提示":   cmd_risk,
    "觸發":       cmd_trigger,
}

HELP_TEXT = (
    "🤖 <b>XAU Elite Bot 指令清單</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📊 今日市場 — 完整分析＋草稿\n"
    "🎯 策略 — H4/H1/M15 操作方向\n"
    "🔍 今日重點關注 — 重點＋風險\n"
    "😎 今日趣事 — 幽默互動貼文\n"
    "📰 今日大新聞 — 財經新聞摘要\n"
    "📅 週報 — 本週市場總結\n"
    "📚 教學貼文 — 外匯基礎教學\n"
    "💬 名言 — 交易金句\n"
    "📊 非農 — NFP 前操作提醒\n"
    "🏦 Fed — 聯準會市場解讀\n"
    "⚠️ 風險提示 — 今日重大風險\n"
    "🔁 觸發 — 執行完整 Orchestrator\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "傳送以上任一關鍵字即可觸發"
)

# ─── 主入口 ───────────────────────────────────────────────

def handle_update(data):
    """接收 Telegram Update JSON，路由到對應 handler"""
    try:
        message = data.get('message') or data.get('channel_post') or {}
        chat_id = message.get('chat', {}).get('id')
        text = (message.get('text') or '').strip()

        if not chat_id or not text:
            return

        print(f"[TG Bot] chat={chat_id} text={text!r}")

        if text in ('help', 'Help', '/help', '幫助', '指令', '/start'):
            _send(chat_id, HELP_TEXT)
            return

        handler = COMMANDS.get(text)
        if handler:
            _send(chat_id, "⏳ 處理中，請稍候...")
            threading.Thread(target=handler, args=(chat_id,), daemon=True).start()
        else:
            _send(chat_id, f"❓ 不認識「{text}」\n\n傳「幫助」查看所有指令")

    except Exception as e:
        print(f"[TG Bot] handle_update 錯誤: {e}")
