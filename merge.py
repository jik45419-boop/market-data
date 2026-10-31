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
import sqlite3
import time

from common import (DATA, META, PACK_LIMIT_MB, STOCK, daily_pack, get_state,
                    gunzip_file, gzip_file, m15_pack, m60_pack, notice, now,
                    open_db, open_meta, pack_dataset, set_state)
from store import get_store

PACKS = DATA / "packs"


def daily_filter(name):
    """數據包名稱 → (種類, 起日, 迄日)"""
    p = name.split("_")
    if name.startswith("daily_stock_"):
        y, h = int(p[2][:4]), p[2][-1]
        if h == "1":
            return ("stock",), f"{y}-01-01", f"{y}-06-30"
        return ("stock",), f"{y}-07-01", f"{y}-12-31"
    return ("etf", "index"), f"{p[3]}-01-01", f"{p[4]}-12-31"


class Packs:
    def __init__(self, store):
        self.store = store
        self.local = {}
        PACKS.mkdir(parents=True, exist_ok=True)

    def path(self, name):
        if name in self.local:
            return self.local[name]
        path = PACKS / f"{name}.db"
        gz = f"{name}.db.gz"
        if self.store.exists(gz):
            self.store.download(gz, PACKS / gz)
            gunzip_file(PACKS / gz, path)
            os.remove(PACKS / gz)
        conn = open_db(path, (pack_dataset(name),))
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


def finalize(store, packs, meta):
    big = []
    for name, path in sorted(packs.local.items()):
        ds = pack_dataset(name)
        key = "date" if ds == "daily" else "dt"
        conn = sqlite3.connect(str(path))
        rows, codes, k0, k1 = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT code), MIN({key}), MAX({key}) FROM {ds}").fetchone()
        conn.execute("VACUUM")
        conn.close()
        size = os.path.getsize(path) / 1e6
        gz = PACKS / f"{name}.db.gz"
        gzip_file(path, gz)
        gz_size = os.path.getsize(gz) / 1e6
        store.upload(gz, f"{name}.db.gz")
        os.remove(gz)
        meta.execute("INSERT OR REPLACE INTO packs VALUES(?,?,?,?,?,?,?,?,?)",
                     (name, ds, rows, codes, str(k0), str(k1),
                      round(size, 1), round(gz_size, 1), now()))
        print(f"上傳 {name}: {rows} 列，{size:.0f} MB（壓縮後 {gz_size:.0f} MB）", flush=True)
        if size > PACK_LIMIT_MB:
            big.append(f"{name} {size:.0f}MB")
    if big:
        notice("數據包超過 200MB", "，".join(big), "warning")


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
    main()
