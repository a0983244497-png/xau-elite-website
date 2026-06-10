# XAU Elite — CLAUDE.md

## 關於 Gino

Gino 朱祐陞，金融業教學助教兼講師，正在打造 XAU Elite 這個黃金交易訊號網站，  
目標是讓更多人學會交易。工作風格：邏輯清楚、效率優先、喜歡推演前因後果。

---

## 溝通原則

- **語言**：繁體中文，技術術語保留英文（API、endpoint、route、session...）
- **語氣**：有活力、直接、不廢話——結論先說，理由後補
- **建議**：完成任務後主動分析下一步，帶出前因後果與策略方向
- **解釋深度**：Gino 理解力強，不需要過度解釋基礎概念

---

## 專案架構

```
/Users/zhuyousheng/xau-elite-website/
├── server.py          # Flask 後端（Railway 部署）
├── index.html         # 今日作戰室（首頁）
├── daily.html         # 日內分析
├── analyzer.html      # AI 圖表分析
├── signals.html       # 訊號紀錄
└── macro.html         # 總經分析
```

**技術棧**
- 後端：Python Flask + PostgreSQL（pg8000）+ gunicorn，部署在 Railway
- 前端：Vanilla HTML / CSS / JS，無框架
- AI：Anthropic Claude API（claude-sonnet-4-6）做市場分析、OpenAI GPT-4o 做圖表視覺分析
- 市場資料：Finnhub API（主）+ yfinance（備援 / 指標）
- GitHub：`a0983244497-png/xau-elite-website`

---

## 開發慣例

### 程式碼風格
- 沿用現有風格（不主動統一縮排或重構不相關部分）
- Python：snake_case，JS：camelCase
- 不加沒必要的註解，只在「為什麼這樣做」不明顯時才加
- 不做任務範圍外的重構或清理

### API 設計
- 所有 API endpoint 回傳 `{"ok": true/false, ...}`
- 失敗回傳 `{"ok": false, "error": "..."}` + 對應 HTTP 狀態碼
- 市場資料優先用 Finnhub，失敗時 fallback 到 yfinance

### 資料庫
- 使用 pg8000.native，每次操作後不需要顯式 close（Railway 環境穩定）
- 重要 table：`daily_analysis`、`signals`、`articles`、`drafts`

### 前端
- 黑金配色（`--gold: #c9a84c`）固定不改
- 所有頁面必須有漢堡選單（mobile ≤960px）
- 支援中 / 英切換：靜態文字加 `data-zh` / `data-en`，動態文字判斷 `currentLang`
- TradingView widget 只在 index.html 的作戰室用，其他頁面用 yfinance 報價卡片

---

## Git 工作流程

- 任務完成後**直接 git push**，不需要再問
- commit message 格式：一行英文摘要（動詞開頭）+ 空行 + 中文條列說明
- 每個 commit 加上：`Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`

---

## 部署

- Railway 自動偵測 git push 觸發部署
- 環境變數（Railway 設定）：
  - `DATABASE_URL` — PostgreSQL 連線字串
  - `ANTHROPIC_API_KEY` — Claude API
  - `OPENAI_KEY` — OpenAI API
  - `FINNHUB_KEY` — Finnhub API
  - `ADMIN_KEY` — 後台管理金鑰（預設 `xauelite2024`）
- 靜態 HTML 由 Flask catchall route `/<path:path>` 服務

---

## 已知限制

- Finnhub 免費方案：`category=forex` 新聞不可靠，要同時抓 `general` + `forex` 再做 keyword filter
- yfinance：`GC=F`（黃金期貨）、`DX-Y.NYB`（DXY）、`^VIX`、`^TNX`（US10Y）、`^IRX`（US2Y）
- Finnhub 經濟日曆的 `time` 欄位可能是 ISO datetime `YYYY-MM-DDTHH:MM:SS` 或純日期 `YYYY-MM-DD`，需要分情況解析
