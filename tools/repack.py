#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重新打包成「連續月份、每包壓縮後約 190MB」（可重複執行）
  來源：年度資料庫 astock_YYYY（可能切片）
  目標：astock_YYYY-MM_YYYY-MM.db.gz，內含 daily、m15、m60
步驟：先決定分包計畫並存進 meta（重跑時沿用同一份）→ 逐包 下載來源 → 按月份篩選合併 → 核對每張表列數 → 上傳 → 記錄
      → 全部完成後才刪除舊的年度資料庫。中途失敗可直接重跑。
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DATA, META, TABLES, get_state, gunzip_file, gzip_file, notice, now,  # noqa: E402
                    open_db, open_meta, pack_get, pack_put, set_state)
from packing import greedy, month_bounds, month_estimates, pack_name, ym_next  # noqa: E402
from store import get_store  # noqa: E402

WORK = DATA / "repack"
YEARLY = re.compile(r"astock_(\d{4})$")


def base(n):
    return re.sub(r"\.db\.gz(\.part\d+)?$", "", n)


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

    years = sorted({int(YEARLY.fullmatch(base(n)).group(1)) for n in store.files if YEARLY.fullmatch(base(n))})
    if not years:
        notice("重新打包", "Drive 上沒有年度資料庫，不用轉換")
        store.upload(Path(__file__).resolve().parent / "restore.py", "restore.py", "text/x-python")
        return

    # 分包計畫（第一次決定後存起來，重跑沿用，避免中途股票清單變動造成重疊）
    raw = get_state(meta, "repack_plan")
    if raw:
        groups = [tuple(g) for g in json.loads(raw)]
        print(f"沿用上次的分包計畫：{len(groups)} 包", flush=True)
    else:
        lo_y, hi_y = years[0], years[-1]
        est = month_estimates(meta)
        months, ym = [], lo_y * 100 + 1
        last_ym = max(k for k in est) if est else hi_y * 100 + 12
        while ym <= min(hi_y * 100 + 12, last_ym):
            months.append(ym); ym = ym_next(ym)
        groups = [(g[0], g[-1]) for g in greedy(months, est)]
        set_state(meta, "repack_plan", json.dumps(groups))
        save_meta(store, meta)
        print(f"分包計畫：{len(groups)} 包：" + "，".join(pack_name(a, b) for a, b in groups), flush=True)

    uses = defaultdict(int)
    for a, b in groups:
        for y in range(a // 100, b // 100 + 1):
            uses[y] += 1
    cache = {}

    def src(y):
        if y not in cache:
            p = WORK / f"src_{y}.db"
            if not pack_get(store, f"astock_{y}", p):
                raise RuntimeError(f"找不到 astock_{y}")
            cache[y] = p
        return cache[y]

    have = {base(n) for n in store.files}
    done_names = {r[0] for r in meta.execute("SELECT name FROM packs")}
    lines, built = [], 0
    for i, (a, b) in enumerate(groups):
        name = pack_name(a, b)
        ys = list(range(a // 100, b // 100 + 1))
        if name in have and name in done_names:
            print(f"{name} 上次已完成，略過", flush=True)
        else:
            out = WORK / f"{name}.db"
            if out.exists():
                out.unlink()
            conn = open_db(out, TABLES)
            d0, d1, t0_, t1_ = month_bounds(a, b, first=(i == 0))
            expect = defaultdict(int)
            for y in ys:
                conn.execute("ATTACH DATABASE ? AS s", (str(src(y)),))
                for t, cond, args in (("daily", "date BETWEEN ? AND ?", (d0, d1)),
                                      ("m15", "dt BETWEEN ? AND ?", (t0_, t1_)),
                                      ("m60", "dt BETWEEN ? AND ?", (t0_, t1_))):
                    expect[t] += conn.execute(f"SELECT COUNT(*) FROM s.{t} WHERE {cond}", args).fetchone()[0]
                    conn.execute(f"INSERT OR REPLACE INTO main.{t} SELECT * FROM s.{t} WHERE {cond}", args)
                conn.commit()
                conn.execute("DETACH DATABASE s")
            got = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
            if any(got[t] != expect[t] for t in TABLES):
                raise RuntimeError(f"{name}: 列數不一致 {got} vs {dict(expect)}，停止（舊資料都還在）")
            codes, dd0, dd1 = conn.execute("SELECT COUNT(DISTINCT code), MIN(date), MAX(date) FROM daily").fetchone()
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
                          sum(got.values()), codes, str(dd0), str(dd1), round(size, 1), round(gz_size, 1), now()))
            save_meta(store, meta)
            built += 1
            lines.append(f"{name} 壓縮 {gz_size:.0f}MB（解壓 {size:.0f}MB）")
            print(f"[{i + 1}/{len(groups)}] {name}：日線 {got['daily']:,}、15分 {got['m15']:,}、60分 {got['m60']:,}；"
                  f"壓縮 {gz_size:.0f}MB／解壓 {size:.0f}MB → {parts} 個檔｜磁碟剩 {shutil.disk_usage(str(DATA)).free / 1e9:.1f}GB",
                  flush=True)
        for y in ys:
            uses[y] -= 1
            if uses[y] == 0 and y in cache:
                os.remove(cache.pop(y))

    # 全部完成後才刪年度資料庫
    removed = 0
    for n in list(store.files):
        if YEARLY.fullmatch(base(n)):
            store.delete(n); removed += 1
    for y in years:
        meta.execute("DELETE FROM packs WHERE name=?", (f"astock_{y}",))
    meta.execute("DELETE FROM state WHERE key='repack_plan'")
    meta.execute("INSERT INTO runs VALUES(?,?)", (now(), f"重新打包成月份區間 {len(groups)} 包"))
    save_meta(store, meta)
    meta.close()
    store.upload(Path(__file__).resolve().parent / "restore.py", "restore.py", "text/x-python")
    sizes = [r for r in lines]
    notice("重新打包", f"共 {len(groups)} 包（這次建立 {built} 個），刪除舊年度檔 {removed} 個；用時 {(time.time() - t0) / 60:.0f} 分鐘")
    if sizes:
        notice("各數據包", "；".join(sizes))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = " | ".join(x.strip() for x in traceback.format_exc().strip().splitlines()[-6:])
        du = shutil.disk_usage(str(DATA))
        print(f"::error title=重新打包失敗::{type(e).__name__}: {e} | {tb} | 磁碟剩 {du.free / 1e9:.1f} GB", flush=True)
        raise
