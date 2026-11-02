#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重新打包成「每年一個資料庫」（可重複執行）
  astock_YYYY.db：當年的 daily（股票＋基金＋指數）、m15、m60 三張表
  壓縮後 ≤ 200MiB → astock_YYYY.db.gz；超過 → astock_YYYY.db.gz.part01、part02…（依序接起來再解壓）
來源可以是任何舊格式（半年／兩年／每季／每月的小包）。
步驟：逐年 下載來源 → 合併 → 核對每張表列數 → 上傳 → 記錄到 meta；全部年份完成後才把舊包移到 Drive 垃圾桶。
中途失敗可直接重跑。
"""
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DAILY_START, DATA, META, PART_BYTES, TABLES, gunzip_file, gzip_file,  # noqa: E402
                    notice, now, open_db, open_meta, pack_get, pack_put)
from store import get_store  # noqa: E402

WORK = DATA / "repack"
Y0 = int(DAILY_START[:4])


def parse(name):
    """舊包名 → (表, 涵蓋年份)；不是舊格式回傳 None"""
    m = re.fullmatch(r"daily_stock_(\d{4})(H[12])?", name)
    if m:
        return "daily", [int(m.group(1))]
    m = re.fullmatch(r"daily_fund_index_(\d{4})_(\d{4})", name)
    if m:
        return "daily", list(range(int(m.group(1)), int(m.group(2)) + 1))
    m = re.fullmatch(r"m15_(\d{4})-\d{2}", name)
    if m:
        return "m15", [int(m.group(1))]
    m = re.fullmatch(r"m60_(\d{4})(Q[1-4]|H[12])", name)
    if m:
        return "m60", [int(m.group(1))]
    return None


def year_filter(table, y):
    """只取這一年的資料（基金＋指數的舊包跨多年）"""
    if table == "daily":
        lo = "0000-00-00" if y == Y0 else f"{y}-01-01"
        return "date BETWEEN ? AND ?", (lo, f"{y}-12-31")
    return "dt / 100000000 = ?", (y,)


def save_meta(store, meta):
    meta.commit()
    gzip_file(META, DATA / "meta.db.gz")
    store.upload(DATA / "meta.db.gz", "meta.db.gz")
    os.remove(DATA / "meta.db.gz")


def main():
    t0 = time.time()
    store = get_store()
    DATA.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    store.download("meta.db.gz", DATA / "meta.db.gz")
    gunzip_file(DATA / "meta.db.gz", META)
    os.remove(DATA / "meta.db.gz")
    meta = open_meta()

    bases = sorted({re.sub(r"\.db\.gz(\.part\d+)?$", "", n) for n in store.files if ".db.gz" in n} - {"meta"})
    old = {b: parse(b) for b in bases if parse(b)}
    by_year = defaultdict(list)
    for b, (t, ys) in old.items():
        for y in ys:
            by_year[max(y, Y0)].append((b, t))
    print(f"Drive 上 {len(bases)} 個數據包，其中舊格式 {len(old)} 個 → 要建立 {len(by_year)} 個年度資料庫", flush=True)

    cache, uses = {}, defaultdict(int)
    for srcs in by_year.values():
        for b, _ in srcs:
            uses[b] += 1

    def local(b):
        if b not in cache:
            p = WORK / f"src_{b}.db"
            pack_get(store, b, p)
            cache[b] = p
        return cache[b]

    lines, built = [], 0
    finished = {r[0] for r in meta.execute("SELECT name FROM packs WHERE name LIKE 'astock_%'")}
    for y in sorted(by_year):
        name = f"astock_{y}"
        if name in finished and name in {re.sub(r"\.db\.gz(\.part\d+)?$", "", n) for n in store.files}:
            print(f"{name} 上次已完成，略過", flush=True)
            for b, _ in by_year[y]:
                uses[b] -= 1
                if uses[b] == 0 and b in cache:
                    os.remove(cache.pop(b))
            continue
        out = WORK / f"{name}.db"
        if out.exists():
            out.unlink()
        conn = open_db(out, TABLES)
        expect = defaultdict(int)
        for b, t in by_year[y]:
            conn.execute("ATTACH DATABASE ? AS s", (str(local(b)),))
            cond, args = year_filter(t, y)
            expect[t] += conn.execute(f"SELECT COUNT(*) FROM s.{t} WHERE {cond}", args).fetchone()[0]
            conn.execute(f"INSERT OR REPLACE INTO main.{t} SELECT * FROM s.{t} WHERE {cond}", args)
            conn.commit()
            conn.execute("DETACH DATABASE s")
            uses[b] -= 1
            if uses[b] == 0:
                os.remove(cache.pop(b))
        got = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        bad = [t for t in TABLES if got[t] != expect.get(t, 0)]
        if bad:
            raise RuntimeError(f"{name}: 列數不一致 {got} vs {dict(expect)}，停止（舊包都還在）")
        codes, d0, d1 = conn.execute("SELECT COUNT(DISTINCT code), MIN(date), MAX(date) FROM daily").fetchone()
        conn.execute("VACUUM")
        conn.close()
        size = os.path.getsize(out) / 1e6
        gz = WORK / f"{name}.db.gz"
        gzip_file(out, gz)
        os.remove(out)
        gz_size = os.path.getsize(gz) / 1e6
        parts = pack_put(store, gz, name)
        os.remove(gz)
        meta.execute("INSERT OR REPLACE INTO packs VALUES(?,?,?,?,?,?,?,?,?)",
                     (name, f"daily {got['daily']}，m15 {got['m15']}，m60 {got['m60']}；{parts} 個檔",
                      sum(got.values()), codes, str(d0), str(d1), round(size, 1), round(gz_size, 1), now()))
        save_meta(store, meta)
        built += 1
        if os.environ.get("TEST_FAIL_AFTER") and built >= int(os.environ["TEST_FAIL_AFTER"]):
            raise RuntimeError("測試：模擬中途失敗")
        line = f"{y}: 日線 {got['daily']:,}、15分 {got['m15']:,}、60分 {got['m60']:,} 列｜解壓 {size:.0f}MB、壓縮 {gz_size:.0f}MB → {parts} 個檔"
        lines.append(line)
        print(f"[{built}/{len(by_year)}] {name} ← {len(by_year[y])} 個舊包｜{line}｜磁碟剩 {__import__('shutil').disk_usage(str(DATA)).free / 1e9:.1f} GB", flush=True)

    # 全部年份都上傳並記錄後，才刪舊包
    for b in old:
        meta.execute("DELETE FROM packs WHERE name=?", (b,))
    meta.execute("INSERT INTO runs VALUES(?,?)", (now(), f"重新打包成年度資料庫 {built} 個"))
    save_meta(store, meta)
    removed = 0
    for n in list(store.files):
        if re.sub(r"\.db\.gz(\.part\d+)?$", "", n) in old:
            store.delete(n)
            removed += 1
    meta.close()
    # 還原工具放在同一個資料夾，下載後直接用
    store.upload(Path(__file__).resolve().parent / "restore.py", "restore.py", "text/x-python")
    n_files = sum(1 for n in store.files if ".db.gz" in n)
    notice("年度資料庫", f"建立 {built} 個、舊檔移到垃圾桶 {removed} 個；Drive 上現在共 {n_files} 個檔（含 meta）；"
                     f"每檔 ≤ {PART_BYTES / 2**20:.0f}MiB；用時 {(time.time() - t0) / 60:.0f} 分鐘")
    if lines:
        notice("各年份", "；".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import shutil
        import traceback
        tb = " | ".join(x.strip() for x in traceback.format_exc().strip().splitlines()[-6:])
        du = shutil.disk_usage(str(DATA))
        print(f"::error title=重新打包失敗::{type(e).__name__}: {e} | {tb} | 磁碟剩 {du.free / 1e9:.1f} GB", flush=True)
        raise
