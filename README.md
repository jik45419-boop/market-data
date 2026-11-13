# A 股全市場回測數據

每天自動從 BaoStock 抓取 A 股全市場數據，分包壓縮後存到你的 Google Drive `market-data` 資料夾。

## 範圍

| 類別 | 內容 | 起始 |
|---|---|---|
| 股票 | 滬深主板＋創業板（**剔除科創板 688/689、北交所**），含 2005 年後退市的股票 | 日線 2005-01-01 |
| ETF | 全部 ETF（只有日線） | 2005-01-01 |
| 指數 | BaoStock 提供的指數（只有日線） | 2005-01-01 |
| 15 分鐘線 | 股票，不復權 | 2020-01-01（BaoStock 上限） |
| 60 分鐘線 | 由 15 分鐘線合成，不復權 | 2020-01-01 |

## Drive 上的檔案

每個數據包是一個 gzip 壓縮的 SQLite 資料庫，涵蓋**連續幾個月**，Drive 上的檔案**盡量接近但不超過 250MB**（解壓後約 800MB），
檔名就是起訖月份：`astock_2025-01_2025-04.db.gz` = 2025 年 1 月～4 月。每個包裡有三張表：
`daily`（股票＋基金＋指數日線）、`m15`（15 分鐘線）、`m60`（60 分鐘線）。

| 期間 | 內容 | 每包大約涵蓋 |
|---|---|---|
| 2005～2019 | 只有日線 | 約 3～4 年 |
| 2020 起 | 日線＋15 分鐘＋60 分鐘 | 約 5～6 個月 |

- 已經結束的包不會再變動；最新的包每天加入新資料，估計會超過 250MB 就開新包（最新包的檔名迄月會跟著改）。
- `meta.db.gz`：股票清單、行業、交易日曆、指數成分、更新進度。
- `restore.py`：解壓工具（`python restore.py` 把資料夾裡的 `.db.gz` 全部解壓成 `.db`）。
  也可以直接用 7-Zip（Windows）或 `gunzip`（Mac／Linux）解壓。

### 日線欄位 `daily`
`code, date, open, high, low, close, preclose, volume, amount, turn, tradestatus, pct_chg, pe_ttm, pb_mrq, ps_ttm, pcf_ttm, is_st, adj_factor`

- 價格都是**不復權**。
- `adj_factor` 是**後復權因子**：後復權價 = 價格 × `adj_factor`
- 前復權價 = 價格 × `adj_factor` ÷ 最新一天的 `adj_factor`
- `tradestatus`：1 正常交易，0 停牌。`is_st`：1 是 ST。

### 分鐘線欄位 `m15` / `m60`
`code, dt, open, high, low, close, volume, amount`
`dt` 是整數 `YYYYMMDDHHMM`，例如 `202403151030` 表示 2024-03-15 10:30 這根 K 線（結束時間）。

### meta.db
| 表 | 內容 |
|---|---|
| `securities` | 代碼、名稱、種類（stock/etf/index）、上市狀態、上市日、退市日 |
| `industry` | 證監會行業分類 |
| `trade_dates` | 交易日曆 |
| `index_members` | 滬深300、中證500、上證50 成分股（最新一期） |
| `packs` | 每個數據包的列數、大小、日期範圍 |
| `progress` | 每檔已更新到哪一天 |
| `runs` | 每次執行的摘要 |

## 讀取範例（Python）

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("astock_2024-01_2024-04.db")      # 先解壓
df = pd.read_sql("SELECT * FROM daily WHERE code='sh.600000'", conn)
df["close_hfq"] = df["close"] * df["adj_factor"]                       # 後復權
df["close_qfq"] = df["close"] * df["adj_factor"] / df["adj_factor"].iloc[-1]  # 前復權
```

## 個股分析報告（tools/analyze.py）

依「買股前個股分析清單」自動算出能量化的部分，輸出 Markdown 報告：

```bash
pip install pandas numpy
python tools/analyze.py 600519 --dir D:/market-data                       # 代碼、sh.600519、600519.SH 或簡體名稱都可以
python tools/analyze.py 600519 --dir D:/market-data --capital 1000000     # 加上總資金，算出建議股數
python tools/analyze.py 600519 --dir D:/market-data --chips 4 --catalyst 3   # 補上人工評分，得到總分與決策
python tools/analyze.py 600519 --dir D:/market-data --date 2025-06-30     # 用歷史某一天當作「今天」
```

- `--dir` 是從 Drive 下載的 market-data 資料夾；只會解壓需要的包（`.db.gz` 或分片都可以，解壓後的 `.db` 留著下次直接用）。
  預設回看 5 年，大約需要最近 12 個包（解壓後約 10GB）；硬碟不夠就加 `--years 3`。
- 報告存到 `reports/<代碼>_<日期>.md`，內容依清單分成七步：

| 步驟 | 自動計算 | 需要人工 |
|---|---|---|
| 第一步 環境 | 滬深300 與年線／季線、站上季線的個股比例、產業 3 個月強弱 | 利率、匯率、政策、產業週期 |
| 第二步 基本面 | 由估值反推的 EPS、每股淨值、每股營收、ROE、淨利率與年增率 | 財報細節、護城河、經營團隊 |
| 第三步 估值 | PE／PB 近 N 年百分位、同業中位數與排名、PEG、三種情境合理價 | 預估 EPS |
| 第四步 籌碼 | 量比、上漲／下跌量、OBV、換手率、漲跌停次數（代理指標） | 融資融券、北向資金、股東人數（`--chips` 評分） |
| 第五步 技術 | 均線、週線、RSI、MACD、KD、布林、ATR、背離、支撐壓力 | — |
| 第六步 風險 | ST、停牌、虧損、估值過高、波動、流動性、過熱等旗標 | 催化劑、反方論點（`--catalyst` 評分） |
| 第七步 計畫 | 停損（支撐或 2×ATR）、目標價、風險報酬比、建議股數（100 股一手、單檔上限 20%） | — |

基本面數字是用 `股價 ÷ PE`、`PB ÷ PE` 等反推的近似值，正式判斷仍要看財報。報告不構成投資建議。

測試：`python tests/analyze_local.py`（用假資料，不連網）。

## 運作方式

GitHub Actions 每天跑 4 次（台灣時間 20:00 / 02:00 / 08:00 / 14:00）：

1. **prepare**：下載 `meta.db`，更新股票清單，確認資料源最新日期
2. **fetch**：4 個分片同時抓資料。每檔記錄「已完成到哪一天」，從隔天抓到最新；每個分片最多 5 小時，沒做完的下次接著做
3. **merge**：寫入對應的數據包，合成 60 分鐘線，壓縮上傳。全部上傳成功後才更新進度，中途失敗下次會自動重做

第一次執行需要補齊歷史（約 140 小時的運算時間），大約 2~3 天完成，之後每天只抓新的交易日。

手動執行：Actions → market-update → Run workflow（可填 `limit` 只測試前 N 檔）。

## 需要的 Secrets

| 名稱 | 說明 |
|---|---|
| `GDRIVE_CLIENT_ID` | Google OAuth 用戶端 ID |
| `GDRIVE_CLIENT_SECRET` | Google OAuth 用戶端密鑰 |
| `GDRIVE_REFRESH_TOKEN` | 權限 `drive.file` 的 refresh token（只能存取本程式建立的檔案） |

## 本地測試

```bash
python tests/run_local.py   # 用假資料跑完整流程，不連網
```
