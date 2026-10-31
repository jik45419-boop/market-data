#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步驟 2：抓資料（多個分片同時跑）
用法：python fetch.py <分片編號>

每檔每種資料記錄「已完成到哪一天」，所以：
- 補歷史和每日更新是同一套邏輯：從上次完成的隔天抓到資料源最新日期
- 時間用完就停，下次執行自動接著做
"""
import os
import sys
import time
from datetime import date

import bsapi
from common import (DAILY_START, DATA, LIMIT, M15_START, SHARDS, STOCK,
                    get_state, next_day, notice, open_db, open_meta, shard_of)

BUDGET_SEC = float(os.environ.get("BUDGET_MIN", "300")) * 60
MAX_FAIL_STREAK = 25


def build_tasks(meta, shard):
    dd = get_state(meta, "daily_data_date")
    md = get_state(meta, "m15_data_date") or dd
    secs = meta.execute(
        "SELECT code, kind, ipo_date, out_date FROM securities "
        "ORDER BY CASE kind WHEN 'stock' THEN 0 WHEN 'etf' THEN 1 ELSE 2 END, code"
    ).fetchall()
    if LIMIT:
        secs = secs[:LIMIT]
    prog = {(c, ds): d for c, ds, d in meta.execute("SELECT * FROM progress")}

    daily, minute = [], []
    for code, kind, ipo, out in secs:
        if shard_of(code) != shard:
            continue
        # 日線
        start = max(DAILY_START, ipo or DAILY_START)
        if (code, "daily") in prog:
            start = max(start, next_day(prog[(code, "daily")]))
        end = min(dd, out) if out else dd
        if start <= end:
            daily.append(("daily", code, kind, start, end))
        # 15 分鐘線（只有股票）
        if kind != STOCK:
            continue
        start = max(M15_START, ipo or M15_START)
        if (code, "m15") in prog:
            start = max(start, next_day(prog[(code, "m15")]))
        end = min(md, out) if out else md
        y = int(start[:4])
        while start <= end and y <= int(end[:4]):
            ye = min(end, f"{y}-12-31")
            if start <= ye:
                minute.append(("m15", code, kind, start, ye))
            y += 1
            start = max(start, f"{y}-01-01")
    # 分鐘線依「年份→代碼」排序：所有分片同時補同一年，合併時只動到少數幾個包
    minute.sort(key=lambda t: (t[3][:4], t[1]))
    return daily + minute


def main():
    shard = int(sys.argv[1])
    t0 = time.time()
    meta = open_meta()
    tasks = build_tasks(meta, shard)
    meta.close()
    n_daily = sum(1 for t in tasks if t[0] == "daily")
    notice(f"分片{shard} 待辦", f"日線 {n_daily} 項，15分鐘線 {len(tasks) - n_daily} 項")
    if not tasks:
        return

    delta = open_db(DATA / f"delta_{shard}.db", ("daily", "m15", "progress"))
    bsapi.login()
    done = rows_d = rows_m = fails = streak = 0
    stopped = ""
    try:
        for i, (ds, code, kind, start, end) in enumerate(tasks):
            if time.time() - t0 > BUDGET_SEC:
                stopped = "時間用完"
                break
            try:
                if ds == "daily":
                    rows = bsapi.daily(code, kind, start, end)
                    delta.executemany(
                        f"INSERT OR REPLACE INTO daily VALUES({','.join('?' * 18)})", rows)
                    rows_d += len(rows)
                else:
                    rows = bsapi.m15(code, start, end)
                    delta.executemany(
                        "INSERT OR REPLACE INTO m15 VALUES(?,?,?,?,?,?,?,?)", rows)
                    rows_m += len(rows)
                delta.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)",
                              (code, ds, end))
                delta.commit()
                done += 1
                streak = 0
            except Exception as e:
                fails += 1
                streak += 1
                print(f"[失敗] {ds} {code} {start}~{end}: {e}", flush=True)
                if streak >= MAX_FAIL_STREAK:
                    stopped = f"連續失敗 {streak} 次，資料源可能異常"
                    break
            if (i + 1) % 200 == 0:
                print(f"進度 {i + 1}/{len(tasks)}，已用 {(time.time() - t0) / 60:.0f} 分鐘",
                      flush=True)
    finally:
        bsapi.logout()
        delta.commit()
        delta.close()

    left = len(tasks) - done - fails
    msg = (f"完成 {done}/{len(tasks)} 項，日線 {rows_d} 列，15分鐘線 {rows_m} 列，"
           f"失敗 {fails}，剩餘 {left}，用時 {(time.time() - t0) / 60:.0f} 分鐘"
           + (f"（{stopped}）" if stopped else ""))
    notice(f"分片{shard} 結果", msg, "warning" if fails or stopped.startswith("連續") else "notice")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notice("執行失敗", f"{type(e).__name__}: {e}", "error")
        raise
