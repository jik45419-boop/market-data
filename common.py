# -*- coding: utf-8 -*-
"""全市場數據：共用設定、分包規則、資料表結構"""
import gzip
import os
import shutil
import sqlite3
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

DAILY_START = "2015-01-01"      # 日線起點
M15_START = "2020-01-01"        # 15 分鐘線起點（BaoStock 最早只到 2020）
SHARDS = int(os.environ.get("SHARDS", "4"))
LIMIT = int(os.environ.get("LIMIT", "0") or 0)   # 測試用：只處理前 N 檔
DATA = Path(os.environ.get("DATA_DIR", "data"))
META = DATA / "meta.db"
PACK_LIMIT_MB = 200

STOCK, ETF, INDEX = "stock", "etf", "index"

DAILY_COLS = ["code", "date", "open", "high", "low", "close", "preclose",
              "volume", "amount", "turn", "tradestatus", "pct_chg",
              "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "is_st", "adj_factor"]
BAR_COLS = ["code", "dt", "open", "high", "low", "close", "volume", "amount"]

SCHEMA = {
    "daily": """CREATE TABLE IF NOT EXISTS daily(
        code TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, preclose REAL,
        volume REAL, amount REAL, turn REAL, tradestatus INTEGER, pct_chg REAL,
        pe_ttm REAL, pb_mrq REAL, ps_ttm REAL, pcf_ttm REAL, is_st INTEGER,
        adj_factor REAL,
        PRIMARY KEY(code, date)) WITHOUT ROWID""",
    "m15": """CREATE TABLE IF NOT EXISTS m15(
        code TEXT NOT NULL, dt INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        PRIMARY KEY(code, dt)) WITHOUT ROWID""",
    "m60": """CREATE TABLE IF NOT EXISTS m60(
        code TEXT NOT NULL, dt INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        PRIMARY KEY(code, dt)) WITHOUT ROWID""",
    "progress": """CREATE TABLE IF NOT EXISTS progress(
        code TEXT NOT NULL, dataset TEXT NOT NULL, done_until TEXT,
        PRIMARY KEY(code, dataset))""",
}

META_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS securities(
        code TEXT PRIMARY KEY, name TEXT, kind TEXT, status INTEGER,
        ipo_date TEXT, out_date TEXT, updated TEXT)""",
    """CREATE TABLE IF NOT EXISTS industry(
        code TEXT PRIMARY KEY, industry TEXT, classification TEXT,
        update_date TEXT)""",
    """CREATE TABLE IF NOT EXISTS trade_dates(
        date TEXT PRIMARY KEY, is_trading INTEGER)""",
    """CREATE TABLE IF NOT EXISTS index_members(
        index_name TEXT, code TEXT, name TEXT, update_date TEXT,
        PRIMARY KEY(index_name, code))""",
    SCHEMA["progress"],
    """CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS packs(
        name TEXT PRIMARY KEY, dataset TEXT, rows INTEGER, codes INTEGER,
        first_key TEXT, last_key TEXT, size_mb REAL, size_gz_mb REAL,
        updated TEXT)""",
    """CREATE TABLE IF NOT EXISTS runs(
        run_at TEXT, summary TEXT)""",
]


def now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def notice(title, msg, level="notice"):
    print(f"{title}: {msg}", flush=True)
    msg = str(msg).replace("\n", " ")[:1000]
    print(f"::{level} title={title}::{msg}", flush=True)


def classify(code, typ):
    """BaoStock type: 1 股票, 2 指數, 5 ETF。剔除科創板與北交所。"""
    if code.startswith("bj."):
        return None
    if typ == "1":
        if code.startswith(("sh.688", "sh.689")):
            return None
        return STOCK
    if typ == "5":
        return ETF
    if typ == "2":
        return INDEX
    return None


def shard_of(code, n=SHARDS):
    return zlib.crc32(code.encode()) % n


def next_day(d):
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


# ---------------- 分包規則（每包解壓後 < 200 MB） ----------------
# 日線股票：每半年一包；ETF＋指數：每 2 年一包
# 15 分鐘線：每月一包；60 分鐘線：每季一包

def fund_index_years(y):
    """基金＋指數日線分組（每包解壓後約 100～130MB）：2015–2020、2021–2024，之後每 2 年一包"""
    if y <= 2020:
        return 2015, 2020
    if y <= 2024:
        return 2021, 2024
    y0 = 2025 + (y - 2025) // 2 * 2
    return y0, y0 + 1


def daily_pack(kind, d):
    """股票日線一年一包（解壓後約 100～170MB）；基金＋指數見 fund_index_years"""
    y = int(d[:4])
    if kind == STOCK:
        return f"daily_stock_{y}"
    y0, y1 = fund_index_years(y)
    return f"daily_fund_index_{y0}_{y1}"


def m15_pack(ym):
    """15 分鐘線一個月一包（解壓後約 60～115MB；兩個月合併會超過 200MB）。ym = YYYYMM 整數"""
    return f"m15_{ym // 100}-{ym % 100:02d}"


def m60_pack(ym):
    """60 分鐘線半年一包（解壓後約 120～150MB）"""
    return f"m60_{ym // 100}H{1 if ym % 100 <= 6 else 2}"


def pack_dataset(name):
    if name.startswith("daily_"):
        return "daily"
    return name[:3]


def open_db(path, tables=()):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    for t in tables:
        conn.execute(SCHEMA[t])
    return conn


def open_meta(path=META):
    conn = sqlite3.connect(str(path))
    for ddl in META_SCHEMA:
        conn.execute(ddl)
    conn.commit()
    return conn


def get_state(conn, key, default=None):
    r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))


def gzip_file(src, dst):
    with open(src, "rb") as a, gzip.open(dst, "wb", compresslevel=6) as b:
        shutil.copyfileobj(a, b, 8 * 1024 * 1024)


def gunzip_file(src, dst):
    with gzip.open(src, "rb") as a, open(dst, "wb") as b:
        shutil.copyfileobj(a, b, 8 * 1024 * 1024)
