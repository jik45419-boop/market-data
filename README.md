# A 股全市場回測數據

每天自動從 BaoStock 抓取 A 股全市場數據，分包壓縮後存到你的 Google Drive `market-data` 資料夾。

## 範圍

| 類別 | 內容 | 起始 |
|---|---|---|
| 股票 | 滬深主板＋創業板（**剔除科創板 688/689、北交所**），含 2015 年後退市的股票 | 日線 2015-01-01 |
| ETF | 全部 ETF（只有日線） | 2015-01-01 |
| 指數 | BaoStock 提供的指數（只有日線） | 2015-01-01 |
| 15 分鐘線 | 股票，不復權 | 2020-01-01（BaoStock 上限） |
| 60 分鐘線 | 由 15 分鐘線合成，不復權 | 2020-01-01 |

## Drive 上的檔案

每個檔案都是 gzip 壓縮的 SQLite 資料庫（`.db.gz`），解壓後每包 < 200 MB。

| 檔名 | 內容 | 分包 |
|---|---|---|
| `daily_stock_2024H1.db.gz` | 股票日線（表 `daily`） | 每半年 |
| `daily_fund_index_2023_2024.db.gz` | ETF＋指數日線（表 `daily`） | 每 2 年 |
| `m15_2024-03.db.gz` | 15 分鐘線（表 `m15`） | 每月 |
| `m60_2024Q1.db.gz` | 60 分鐘線（表 `m60`） | 每季 |
| `meta.db.gz` | 股票清單、行業、交易日曆、指數成分、更新進度 | — |

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
import gzip, shutil, sqlite3
import pandas as pd

def open_pack(path_gz):
    path = path_gz[:-3]
    with gzip.open(path_gz, "rb") as a, open(path, "wb") as b:
        shutil.copyfileobj(a, b)
    return sqlite3.connect(path)

conn = open_pack("daily_stock_2024H1.db.gz")
df = pd.read_sql("SELECT * FROM daily WHERE code='sh.600000'", conn)
df["close_hfq"] = df["close"] * df["adj_factor"]                       # 後復權
df["close_qfq"] = df["close"] * df["adj_factor"] / df["adj_factor"].iloc[-1]  # 前復權
```

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
