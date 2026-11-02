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

**每年一個 SQLite 資料庫** `astock_YYYY.db`，裡面有三張表：`daily`（股票＋基金＋指數日線）、`m15`（15 分鐘線）、`m60`（60 分鐘線）。
壓縮後（`.db.gz`）超過 200MiB 就切成 `astock_YYYY.db.gz.part01`、`part02`…，每片 200MiB，**要把同一年的所有片段接起來再解壓**。

| 檔名 | 內容 |
|---|---|
| `astock_2015.db.gz` ～ `astock_2019.db.gz` | 只有日線（15 分鐘線從 2020 年起），每年一個檔 |
| `astock_2025.db.gz.part01`～`part03` | 2025 年：日線、15 分鐘線、60 分鐘線（解壓後約 1.6 GB） |
| `meta.db.gz` | 股票清單、行業、交易日曆、指數成分、更新進度 |
| `restore.py` | 還原工具：把片段接起來並解壓 |

### 還原
把同一年的所有片段和 `restore.py` 下載到同一個資料夾，然後：
```
python restore.py            # 還原資料夾裡所有年份 → astock_YYYY.db
python restore.py 2025       # 只還原 2025 年
```
不用程式也可以：Mac／Linux `cat astock_2025.db.gz.part* | gunzip > astock_2025.db`；
Windows `copy /b astock_2025.db.gz.part01+astock_2025.db.gz.part02+astock_2025.db.gz.part03 astock_2025.db.gz`，再用 7-Zip 解壓。

每天的更新只會改到今年（跨年時加上去年）的檔案，舊年份不會再變動。

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

conn = sqlite3.connect("astock_2024.db")      # 先用 restore.py 還原
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
