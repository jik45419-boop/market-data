#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步驟 3：合併
- 把各分片抓到的資料寫進對應的數據包（只下載會被改到的包）
- 由 15 分鐘線合成 60 分鐘線
- 壓縮後上傳到 Drive，最後才更新進度（中途失敗下次會自動重做）
"""
import glob
import os
import re
import sqlite3
import time

from common import (DAILY_START, DATA, META, PART_BYTES, STOCK, TABLES, daily_pack, get_state,
                    gzip_file, m15_pack, m60_pack, notice, now, open_db, open_meta,
                    pack_get, pack_put, pack_year, set_state)
from store import get_store

PACKS = DATA / "packs"
DAILY_START_YEAR = DAILY_START[:4]


def daily_filter(name):
    """數據包名稱 → (種類, 起日, 迄日)：每年一包，股票＋基金＋指數"""
    y = pack_year(name)
    lo = "1990-01-01" if f"{y}" == DAILY_START_YEAR else f"{y}-01-01"
    return ("stock", "etf", "index"), lo, f"{y}-12-31"


class Packs:
    def __init__(self, store):
        self.store = store
        self.local = {}
        PACKS.mkdir(parents=True, exist_ok=True)

    def path(self, name):
        if name in self.local:
            return self.local[name]
        path = PACKS / f"{name}.db"
        pack_get(self.store, name, path)
        conn = open_db(path, TABLES)
        conn.close()
        self.local[name] = path
        return path

    def run(self, name, delta, sql_list):
        conn = sqlite3.connect(str(self.path(name)))
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("ATTACH DATABASE ? AS d", (str(delta),))
        conn.execute("ATTACH DATABASE ? AS m", (str(META),))
        n = 0
        for sql, args in sql_list:
            n += conn.execute(sql, args).rowcount or 0
        conn.commit()
        conn.close()
        return n


def merge_delta(packs, delta):
    src = sqlite3.connect(str(delta))
    src.execute("ATTACH DATABASE ? AS m", (str(META),))
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    daily_groups = src.execute(
        "SELECT DISTINCT s.kind, substr(x.date,1,10) FROM daily x "
        "JOIN m.securities s ON s.code=x.code") if "daily" in tables else []
    names = sorted({daily_pack(k, d) for k, d in daily_groups})
    months = [r[0] for r in src.execute("SELECT DISTINCT dt/1000000 FROM m15")] \
        if "m15" in tables else []
    src.close()

    stats = {"daily": 0, "m15": 0, "m60": 0}
    for name in names:
        kinds, lo, hi = daily_filter(name)
        q = ("INSERT OR REPLACE INTO daily SELECT x.* FROM d.daily x "
             "JOIN m.securities s ON s.code=x.code "
             f"WHERE s.kind IN ({','.join('?' * len(kinds))}) AND x.date BETWEEN ? AND ?")
        stats["daily"] += packs.run(name, delta, [(q, (*kinds, lo, hi))])

    for ym in sorted(months):
        lo, hi = ym * 1000000, ym * 1000000 + 999999
        stats["m15"] += packs.run(m15_pack(ym), delta, [(
            "INSERT OR REPLACE INTO m15 SELECT * FROM d.m15 WHERE dt BETWEEN ? AND ?",
            (lo, hi))])
        stats["m60"] += packs.run(m60_pack(ym), delta, [
            ("DROP TABLE IF EXISTS temp.b", ()),
            ("""CREATE TEMP TABLE b AS
                SELECT code, dt, open, high, low, close, volume, amount,
                  (dt / 10000) * 10000 + CASE
                    WHEN dt % 10000 <= 1030 THEN 1030
                    WHEN dt % 10000 <= 1130 THEN 1130
                    WHEN dt % 10000 <= 1400 THEN 1400
                    ELSE 1500 END AS g
                FROM d.m15 WHERE dt BETWEEN ? AND ?""", (lo, hi)),
            ("""INSERT OR REPLACE INTO m60
                SELECT a.code, a.g, f.open, a.high, a.low, l.close, a.volume, a.amount
                FROM (SELECT code, g, max(high) AS high, min(low) AS low,
                             sum(volume) AS volume, sum(amount) AS amount
                      FROM b GROUP BY code, g) a
                JOIN (SELECT code, g, min(dt), open FROM b GROUP BY code, g) f
                  ON f.code = a.code AND f.g = a.g
                JOIN (SELECT code, g, max(dt), close FROM b GROUP BY code, g) l
                  ON l.code = a.code AND l.g = a.g""", ()),
        ])
    return stats


def pack_info(path):
    """各表列數、股票數、日期範圍"""
    conn = sqlite3.connect(str(path))
    n = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    codes, d0, d1 = conn.execute("SELECT COUNT(DISTINCT code), MIN(date), MAX(date) FROM daily").fetchone()
    conn.close()
    return n, codes, d0, d1


def finalize(store, packs, meta):
    for name, path in sorted(packs.local.items()):
        n, codes, d0, d1 = pack_info(path)
        conn = sqlite3.connect(str(path))
        conn.execute("VACUUM")
        conn.close()
        size = os.path.getsize(path) / 1e6
        gz = PACKS / f"{name}.db.gz"
        gzip_file(path, gz)
        gz_size = os.path.getsize(gz) / 1e6
        parts = pack_put(store, gz, name)
        os.remove(gz)
        meta.execute("INSERT OR REPLACE INTO packs VALUES(?,?,?,?,?,?,?,?,?)",
                     (name, f"daily {n['daily']}，m15 {n['m15']}，m60 {n['m60']}；{parts} 個檔",
                      sum(n.values()), codes, str(d0), str(d1), round(size, 1), round(gz_size, 1), now()))
        print(f"上傳 {name}: 日線 {n['daily']:,}、15分 {n['m15']:,}、60分 {n['m60']:,} 列；"
              f"解壓 {size:.0f} MB、壓縮 {gz_size:.0f} MB → {parts} 個檔（每檔 ≤ {PART_BYTES / 2**20:.0f}MiB）", flush=True)


def coverage(meta):
    dd = get_state(meta, "daily_data_date")
    md = get_state(meta, "m15_data_date") or dd
    d_total, d_done = meta.execute(
        """SELECT COUNT(*), SUM(p.done_until >= MIN(?, COALESCE(s.out_date, ?)))
           FROM securities s LEFT JOIN progress p
             ON p.code = s.code AND p.dataset = 'daily'""", (dd, dd)).fetchone()
    m_total, m_done = meta.execute(
        """SELECT COUNT(*), SUM(p.done_until >= MIN(?, COALESCE(s.out_date, ?)))
           FROM securities s LEFT JOIN progress p
             ON p.code = s.code AND p.dataset = 'm15'
           WHERE s.kind = ? AND COALESCE(s.out_date, '9999') >= '2020-01-01'""",
        (md, md, STOCK)).fetchone()
    return (f"日線 {d_done or 0}/{d_total} 檔已到 {dd}；"
            f"15分鐘線 {m_done or 0}/{m_total} 檔已到 {md}")


def main():
    t0 = time.time()
    store = get_store()
    old = [n for n in store.files if re.match(r"(daily_stock|daily_fund_index|m15|m60)_", n)]
    if old:   # 舊格式還沒轉完：這次不合併（進度不更新，下次會重抓），避免新舊格式混在一起
        notice("略過合併", f"Drive 上還有 {len(old)} 個舊格式數據包，等重新打包（repack）完成後再合併", "warning")
        return
    packs = Packs(store)
    deltas = sorted(glob.glob(str(DATA / "deltas" / "**" / "delta_*.db"), recursive=True))
    total = {"daily": 0, "m15": 0, "m60": 0}
    for delta in deltas:
        s = merge_delta(packs, delta)
        for k in total:
            total[k] += s[k]
        print(f"合併 {os.path.basename(delta)}: {s}", flush=True)

    meta = open_meta()
    finalize(store, packs, meta)

    # 數據包都上傳成功後，才記錄進度
    for delta in deltas:
        meta.execute("ATTACH DATABASE ? AS d", (delta,))
        meta.execute("""INSERT INTO progress SELECT code, dataset, done_until
                        FROM d.progress WHERE true
                        ON CONFLICT(code, dataset) DO UPDATE SET
                        done_until = MAX(done_until, excluded.done_until)""")
        meta.commit()
        meta.execute("DETACH DATABASE d")

    cov = coverage(meta)
    summary = (f"新增/更新 日線 {total['daily']} 列，15分 {total['m15']} 列，"
               f"60分 {total['m60']} 列，數據包 {len(packs.local)} 個，"
               f"用時 {(time.time() - t0) / 60:.0f} 分鐘")
    set_state(meta, "last_merge", now())
    meta.execute("INSERT INTO runs VALUES(?,?)", (now(), summary + "；" + cov))
    meta.commit()
    meta.close()

    gzip_file(META, DATA / "meta.db.gz")
    store.upload(DATA / "meta.db.gz", "meta.db.gz")
    notice("本次合併", summary)
    notice("整體進度", cov)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notice("執行失敗", f"{type(e).__name__}: {e}", "error")
        raise
