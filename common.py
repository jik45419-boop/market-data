# -*- coding: utf-8 -*-
"""全市場數據：共用設定、分包規則、資料表結構"""
import gzip
import os
import shutil
import sqlite3
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

DAILY_START = "2005-01-01"      # 日線起點（2026-11 起由 2015 往前延伸到 2005）
BACKFILL_END = "2014-12-31"     # 2015 年以前的日線屬於「回補」，進度另外記錄為 daily_old
M15_START = "2020-01-01"        # 15 分鐘線起點（BaoStock 最早只到 2020）
SHARDS = int(os.environ.get("SHARDS", "4"))
LIMIT = int(os.environ.get("LIMIT", "0") or 0)   # 測試用：只處理前 N 檔
DATA = Path(os.environ.get("DATA_DIR", "data"))
META = DATA / "meta.db"
PACK_LIMIT_MB = 200
# Drive 上每個檔案最多 200MiB：壓縮後超過就切成 .part01、.part02…（測試時可用 PART_MB 改小）
# 保險：萬一某個包壓縮後仍超過 250MB，就切成 .part01、.part02…（正常不會發生；測試時可用 PART_MB 改小）
PART_BYTES = int(float(os.environ["PART_MB"]) * 1024 * 1024) if os.environ.get("PART_MB") else 250_000_000
TABLES = ("daily", "m15", "m60")

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

# ---------------------------------------------------------------- Drive 上的數據包（單檔或分片）
def pack_files(store, name):
    """這個數據包在 Drive 上的檔名：[name.db.gz] 或 [name.db.gz.part01, ...]"""
    parts = sorted(n for n in store.files if n.startswith(f"{name}.db.gz.part"))
    if parts:
        return parts
    return [f"{name}.db.gz"] if f"{name}.db.gz" in store.files else []


def pack_get(store, name, db_path):
    """下載（分片就依序接起來）並解壓到 db_path；Drive 上沒有就回傳 False"""
    files = pack_files(store, name)
    if not files:
        return False
    gz = Path(str(db_path) + ".gz")
    with open(gz, "wb") as out:
        for f in files:
            tmp = Path(str(db_path) + ".part")
            store.download(f, tmp)
            with open(tmp, "rb") as a:
                shutil.copyfileobj(a, out, 8 * 1024 * 1024)
            os.remove(tmp)
    gunzip_file(gz, db_path)
    os.remove(gz)
    return True


def pack_put(store, gz_path, name):
    """上傳壓縮檔：不超過 200MiB 就一個檔，超過就切片；刪掉 Drive 上多出來的舊檔。回傳上傳的檔案數"""
    size = os.path.getsize(gz_path)
    old = set(pack_files(store, name))
    new = []
    if size <= PART_BYTES:
        store.upload(gz_path, f"{name}.db.gz")
        new.append(f"{name}.db.gz")
    else:
        with open(gz_path, "rb") as a:
            i = 0
            while True:
                chunk = a.read(PART_BYTES)
                if not chunk:
                    break
                i += 1
                fn = f"{name}.db.gz.part{i:02d}"
                tmp = Path(str(gz_path) + f".part{i:02d}")
                tmp.write_bytes(chunk)
                store.upload(tmp, fn)
                os.remove(tmp)
                new.append(fn)
    for f in old - set(new):
        store.delete(f)
    return len(new)


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
