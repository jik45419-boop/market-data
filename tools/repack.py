#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重新打包（可重複執行）：把現有的數據包重新切成「連續月份、壓縮後盡量接近但不超過 250MB」
  來源：現有的區間包 astock_YYYY-MM_YYYY-MM（或舊的年度包 astock_YYYY）
  做法：從最早的月份開始，依估算一次放入盡量多的月份 → 建包 → 實際壓縮後若超過 250MB，少放一個月重做
        → 上傳、記錄 → 下一包從下個月開始
  來源清單和已完成的新包都記在 meta，中途失敗重跑會接著做；全部完成後才刪除舊包
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DATA, META, TABLES, get_state, gunzip_file, gzip_file, notice, now,  # noqa: E402
                    open_db, open_meta, pack_get, pack_put, set_state)
from packing import LIMIT, TARGET, month_bounds, month_estimates, pack_name, parse, ym_next  # noqa: E402
from store import get_store  # noqa: E402

WORK = DATA / "repack"
YEARLY = re.compile(r"astock_(\d{4})$")


def base(n):
    return re.sub(r"\.db\.gz(\.part\d+)?$", "", n)


def span(name):
    """包名 → (起月, 迄月)"""
    p = parse(name)
    if p:
        return p
    y = int(YEARLY.fullmatch(name).group(1))
    return y * 100 + 1, y * 100 + 12


def save(store, meta):
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

    raw = get_state(meta, "repack_sources")
    if raw:
        sources = json.loads(raw)
        built = [tuple(x) for x in json.loads(get_state(meta, "repack_built") or "[]")]
        print(f"接續上次：來源 {len(sources)} 包，已完成 {len(built)} 包", flush=True)
    else:
        sources = sorted({base(n) for n in store.files if parse(base(n)) or YEARLY.fullmatch(base(n))},
                         key=lambda n: span(n)[0])
        if not sources:
            notice("重新打包", "Drive 上沒有數據包"); return
        built = []
        set_state(meta, "repack_sources", json.dumps(sources))
        set_state(meta, "repack_built", "[]")
        save(store, meta)
    spans = {n: span(n) for n in sources}
    first_ym = min(a for a, _ in spans.values())
    last_ym = max(b for _, b in spans.values())
    est = month_estimates(meta)
    print(f"來源 {len(sources)} 包（{first_ym}～{last_ym}）；目標每包估算 {TARGET / 1e6:.0f}MB、上限 {LIMIT / 1e6:.0f}MB", flush=True)

    cache = {}

    def src(n):
        if n not in cache:
            p = WORK / f"src_{n}.db"
            if not pack_get(store, n, p):
                raise RuntimeError(f"找不到來源 {n}")
            cache[n] = p
        return cache[n]

    def build(lo, hi, first):
        name = pack_name(lo, hi)
        out = WORK / f"{name}.db"
        if out.exists():
            out.unlink()
        conn = open_db(out, TABLES)
        d0, d1, t0_, t1_ = month_bounds(lo, hi, first=first)
        expect = {t: 0 for t in TABLES}
        for n in sources:
            a, b = spans[n]
            if b < lo or a > hi:
                continue
            conn.execute("ATTACH DATABASE ? AS s", (str(src(n)),))
            for t, args in (("daily", (d0, d1)), ("m15", (t0_, t1_)), ("m60", (t0_, t1_))):
                cond = "date BETWEEN ? AND ?" if t == "daily" else "dt BETWEEN ? AND ?"
                expect[t] += conn.execute(f"SELECT COUNT(*) FROM s.{t} WHERE {cond}", args).fetchone()[0]
                conn.execute(f"INSERT OR REPLACE INTO main.{t} SELECT * FROM s.{t} WHERE {cond}", args)
            conn.commit()
            conn.execute("DETACH DATABASE s")
        got = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        if got != expect:
            raise RuntimeError(f"{name}: 列數不一致 {got} vs {expect}，停止（舊資料都還在）")
        codes, dd0, dd1 = conn.execute("SELECT COUNT(DISTINCT code), MIN(date), MAX(date) FROM daily").fetchone()
        conn.execute("VACUUM")
        conn.close()
        size = os.path.getsize(out)
        gz = WORK / f"{name}.db.gz"
        gzip_file(out, gz)
        os.remove(out)
        return name, gz, got, codes, dd0, dd1, size

    lines = []
    nxt = ym_next(max(b for _, b in built)) if built else first_ym
    while nxt <= last_ym:
        # 依估算放入盡量多的月份
        months, size_est, m = [], 0.0, nxt
        while m <= last_ym:
            e = est.get(m, 0)
            if months and size_est + e > TARGET:
                break
            months.append(m); size_est += e; m = ym_next(m)
        while True:
            name, gz, got, codes, dd0, dd1, size = build(months[0], months[-1], first=(months[0] == first_ym))
            gz_size = os.path.getsize(gz)
            if gz_size <= LIMIT or len(months) == 1:
                break
            print(f"{name} 壓縮後 {gz_size / 1e6:.0f}MB 超過上限，少放一個月重做", flush=True)
            os.remove(gz); months.pop()
        parts = pack_put(store, gz, name)
        os.remove(gz)
        meta.execute("INSERT OR REPLACE INTO packs VALUES(?,?,?,?,?,?,?,?,?)",
                     (name, f"daily {got['daily']}，m15 {got['m15']}，m60 {got['m60']}；{parts} 個檔",
                      sum(got.values()), codes, str(dd0), str(dd1), round(size / 1e6, 1), round(gz_size / 1e6, 1), now()))
        built.append((months[0], months[-1]))
        set_state(meta, "repack_built", json.dumps(built))
        save(store, meta)
        lines.append(f"{name} {gz_size / 1e6:.0f}MB")
        print(f"[{len(built)}] {name}：日線 {got['daily']:,}、15分 {got['m15']:,}、60分 {got['m60']:,}；"
              f"壓縮 {gz_size / 1e6:.0f}MB（估算 {size_est / 1e6:.0f}MB）／解壓 {size / 1e6:.0f}MB｜"
              f"磁碟剩 {shutil.disk_usage(str(DATA)).free / 1e9:.1f}GB", flush=True)
        nxt = ym_next(months[-1])
        if os.environ.get("TEST_FAIL_AFTER") and len(built) >= int(os.environ["TEST_FAIL_AFTER"]):
            raise RuntimeError("測試：模擬中途失敗")
        for n in list(cache):                 # 用不到的來源先刪掉，省磁碟
            if spans[n][1] < nxt:
                os.remove(cache.pop(n))

    # 全部完成後才刪舊包（新包同名的不刪）
    new_names = {pack_name(a, b) for a, b in built}
    removed = 0
    for n in list(store.files):
        b = base(n)
        if b in sources and b not in new_names:
            store.delete(n); removed += 1
    for n in sources:
        if n not in new_names:
            meta.execute("DELETE FROM packs WHERE name=?", (n,))
    meta.execute("DELETE FROM state WHERE key IN ('repack_sources','repack_built','repack_plan')")
    meta.execute("INSERT INTO runs VALUES(?,?)", (now(), f"重新打包：{len(sources)} 包 → {len(built)} 包"))
    save(store, meta)
    meta.close()
    store.upload(Path(__file__).resolve().parent / "restore.py", "restore.py", "text/x-python")
    notice("重新打包", f"{len(sources)} 包 → {len(built)} 包，刪除舊檔 {removed} 個；用時 {(time.time() - t0) / 60:.0f} 分鐘")
    if lines:
        notice("各數據包", "；".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = " | ".join(x.strip() for x in traceback.format_exc().strip().splitlines()[-6:])
        du = shutil.disk_usage(str(DATA))
        print(f"::error title=重新打包失敗::{type(e).__name__}: {e} | {tb} | 磁碟剩 {du.free / 1e9:.1f} GB", flush=True)
        raise
