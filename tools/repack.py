#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性重新打包（可重複執行）：把 Drive 上舊規則的小數據包合併成新規則的包
  · 股票日線：半年一包 → 一年一包
  · 基金＋指數日線：兩年一包 → 2015–2020、2021–2024、之後每 2 年一包
  · 60 分鐘線：每季一包 → 半年一包
  · 15 分鐘線：維持每月一包（兩個月合併會超過 200MB）
每個新包：下載對應的舊包 → 合併 → 核對列數 → 上傳新包 → 更新 meta → 把舊包移到 Drive 垃圾桶
中途失敗可以直接重跑：已經上傳的新包會被當作來源之一一起合併。
"""
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DATA, META, PACK_LIMIT_MB, STOCK, fund_index_years, gunzip_file,  # noqa: E402
                    gzip_file, notice, now, open_db, open_meta, pack_dataset)
from store import get_store  # noqa: E402

PACKS = DATA / "repack"


def target(name):
    """舊（或新）包名 → 新包名"""
    if name.startswith("daily_stock_"):
        return f"daily_stock_{name.split('_')[2][:4]}"
    if name.startswith("daily_fund_index_"):
        y0 = int(name.split("_")[3])
        a, b = fund_index_years(y0)
        return f"daily_fund_index_{a}_{b}"
    if name.startswith("m60_"):
        y, part = name[4:8], name[8:]
        if part.startswith("Q"):
            return f"m60_{y}H{1 if int(part[1]) <= 2 else 2}"
        return name
    return name


def fetch_meta(store):
    DATA.mkdir(parents=True, exist_ok=True)
    store.download("meta.db.gz", DATA / "meta.db.gz")
    gunzip_file(DATA / "meta.db.gz", META)
    os.remove(DATA / "meta.db.gz")


def save_meta(store, meta):
    meta.commit()
    gzip_file(META, DATA / "meta.db.gz")
    store.upload(DATA / "meta.db.gz", "meta.db.gz")
    os.remove(DATA / "meta.db.gz")


def main():
    t0 = time.time()
    store = get_store()
    fetch_meta(store)
    meta = open_meta()
    PACKS.mkdir(parents=True, exist_ok=True)

    names = sorted(n[:-6] for n in store.files if n.endswith(".db.gz") and n != "meta.db.gz")
    groups = defaultdict(list)
    for n in names:
        groups[target(n)].append(n)
    todo = {t: srcs for t, srcs in groups.items() if srcs != [t]}
    print(f"Drive 上 {len(names)} 個數據包 → 新規則 {len(groups)} 個；需要重新打包 {len(todo)} 個", flush=True)

    big, done = [], 0
    for tgt, srcs in sorted(todo.items()):
        ds = pack_dataset(tgt)
        key = "date" if ds == "daily" else "dt"
        out = PACKS / f"{tgt}.new.db"
        if out.exists():
            out.unlink()
        conn = open_db(out, (ds,))
        expect = 0
        for src in srcs:
            gz, db = PACKS / f"{src}.db.gz", PACKS / f"{src}.db"
            store.download(f"{src}.db.gz", gz)
            gunzip_file(gz, db)
            os.remove(gz)
            conn.execute("ATTACH DATABASE ? AS s", (str(db),))
            n = conn.execute(f"SELECT COUNT(*) FROM s.{ds}").fetchone()[0]
            expect += n
            conn.execute(f"INSERT OR REPLACE INTO main.{ds} SELECT * FROM s.{ds}")
            conn.commit()
            conn.execute("DETACH DATABASE s")
            os.remove(db)
        rows, codes, k0, k1 = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT code), MIN({key}), MAX({key}) FROM {ds}").fetchone()
        if rows != expect:
            raise RuntimeError(f"{tgt}: 合併後 {rows} 列，來源合計 {expect} 列，不一致，停止（舊包都還在）")
        conn.execute("VACUUM")
        conn.close()
        size = os.path.getsize(out) / 1e6
        gz = PACKS / f"{tgt}.db.gz"
        gzip_file(out, gz)
        gz_size = os.path.getsize(gz) / 1e6
        store.upload(gz, f"{tgt}.db.gz")
        os.remove(gz)
        os.remove(out)
        meta.execute("INSERT OR REPLACE INTO packs VALUES(?,?,?,?,?,?,?,?,?)",
                     (tgt, ds, rows, codes, str(k0), str(k1), round(size, 1), round(gz_size, 1), now()))
        for src in srcs:
            if src != tgt:
                meta.execute("DELETE FROM packs WHERE name=?", (src,))
        save_meta(store, meta)                      # 先記錄新包，再刪舊包
        for src in srcs:
            if src != tgt:
                store.delete(f"{src}.db.gz")
        done += 1
        if size > PACK_LIMIT_MB:
            big.append(f"{tgt} {size:.0f}MB")
        print(f"[{done}/{len(todo)}] {tgt} ← {', '.join(srcs)}：{rows:,} 列，{size:.0f} MB（壓縮 {gz_size:.0f} MB）", flush=True)

    left = meta.execute("SELECT COUNT(*), SUM(size_mb), MAX(size_mb), SUM(size_gz_mb) FROM packs").fetchone()
    meta.execute("INSERT INTO runs VALUES(?,?)", (now(), f"重新打包 {done} 個"))
    save_meta(store, meta)
    meta.close()
    notice("重新打包", f"完成 {done} 個新包；現在共 {left[0]} 個數據包，解壓後合計 {left[1]:,.0f} MB、"
                   f"最大 {left[2]:.0f} MB，壓縮後合計 {left[3]:,.0f} MB；用時 {(time.time() - t0) / 60:.0f} 分鐘")
    if big:
        notice("數據包超過 200MB", "，".join(big), "warning")


if __name__ == "__main__":
    main()
