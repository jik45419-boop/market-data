#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市場寬度：用假的數據包驗證漲跌停判斷、均線、新高新低、騰落線、分兩次增量計算結果一致"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import breadth  # noqa: E402
from common import SCHEMA, gzip_file  # noqa: E402
from store import LocalStore  # noqa: E402

tmp = Path(tempfile.mkdtemp())
store = LocalStore(tmp / "drive")


def days(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


D = days("2020-01-02", 320)          # 2020-01-02 起 320 個交易日（跨過 2020-08-24）


def rows():
    out = []
    # A：每天漲 0.1%，一路創新高、站上所有均線
    p = 10.0
    for d in D:
        pc, p = p, round(p * 1.001, 4)
        out.append(("sh.600001", d, p, pc, 1e6, 1, 0, 1.0))
    # B：每天跌 0.1%，一路創新低
    p = 10.0
    for d in D:
        pc, p = p, round(p * 0.999, 4)
        out.append(("sz.000001", d, p, pc, 1e6, 1, 0, 1.0))
    # C：創業板；第 50 天漲停 10%（2020-08-24 前），第 300 天漲停 20%，第 301 天跌 18%（不是跌停）
    p = 10.0
    for i, d in enumerate(D):
        pc = p
        if i == 50:
            p = round(pc * 1.10, 2)
        elif i == 300:
            p = round(pc * 1.20, 2)
        elif i == 301:
            p = round(pc * 0.82, 2)
        out.append(("sz.300001", d, p, pc, 1e6, 1, 0, 1.0))
    # D：ST 股，第 100 天漲 5%（漲停），第 101 天跌 5%（跌停）
    p = 4.0
    for i, d in enumerate(D):
        pc = p
        if i == 100:
            p = round(pc * 1.05, 2)
        elif i == 101:
            p = round(pc * 0.95, 2)
        out.append(("sh.600002", d, p, pc, 1e6, 1, 1, 1.0))
    # E：第 200 天才上市，第一天漲 44%（新股，不算漲停）；第 210 天漲停 10%
    p = 20.0
    for i, d in enumerate(D[200:], start=200):
        pc = p
        if i == 200:
            p = round(pc * 1.44, 2)
        elif i == 210:
            p = round(pc * 1.10, 2)
        out.append(("sh.600003", d, p, pc, 1e6, 1, 0, 1.0))
    # ETF、指數：不應計入
    for d in D:
        out.append(("sh.510300", d, 4.0, 4.0, 1e9, 1, 0, 1.0))
        out.append(("sh.000300", d, 4000.0, 4000.0, 1e12, 1, 0, 1.0))
    return out


def make_pack(name, lo, hi, data):
    db = tmp / f"{name}.db"
    c = sqlite3.connect(db)
    c.execute(SCHEMA["daily"])
    c.executemany("insert into daily(code,date,close,preclose,amount,tradestatus,is_st,adj_factor) values(?,?,?,?,?,?,?,?)",
                  [r for r in data if lo <= r[1] <= hi])
    c.commit()
    c.close()
    gzip_file(db, tmp / "drive" / f"{name}.db.gz")
    store.files[f"{name}.db.gz"] = tmp / "drive" / f"{name}.db.gz"


data = rows()
make_pack("astock_2020-01_2020-06", "2020-01-01", "2020-06-31", data)
make_pack("astock_2020-07_2021-03", "2020-07-01", "2021-03-31", data)


def run(dbfile):
    con = sqlite3.connect(dbfile)
    n = breadth.update(store, con, tmp / "work", log=lambda *_: None)
    return con, n


# 一次算完
con, n = run(tmp / "a.db")
assert n == len(D), n
r = {row[0]: row for row in con.execute('select "期間","涨停家数","跌停家数","交易家数","站上250日线%","250日新高家数","250日新低家数","腾落线ADL","上涨家数","下跌家数" from market_breadth')}


def at(i):
    return r[D[i]]


assert at(50)[1] == 1, at(50)                 # 創業板 10% 漲停（2020-08-24 前）
assert at(100)[1] == 1 and at(101)[2] == 1    # ST 5% 漲停、跌停
assert at(200)[1] == 0, at(200)               # 新股首日不算
assert at(210)[1] == 1, at(210)
assert D[300] >= "2020-08-24" and at(300)[1] == 1, (D[300], at(300))   # 創業板 20% 漲停
assert at(301)[2] == 0, at(301)               # 跌 18% 不是跌停
assert at(299)[3] == 5 and at(0)[3] == 4      # ETF、指數不算；E 第 200 天起才有
assert at(248)[5] == 0 and at(249)[5] >= 1   # 第 250 天起才有 250 日新高
assert at(260)[5] >= 1 and at(260)[6] >= 1
total = sum(row[8] - row[9] for row in r.values())
assert at(len(D) - 1)[7] == total            # 騰落線＝累計（上漲－下跌）

# 分兩次：先只有第一個包，再加第二個包（模擬每日增量），結果要一樣
store2 = LocalStore(tmp / "drive2")
import shutil  # noqa: E402
shutil.copy(tmp / "drive" / "astock_2020-01_2020-06.db.gz", tmp / "drive2")
store2.files = {"astock_2020-01_2020-06.db.gz": tmp / "drive2" / "astock_2020-01_2020-06.db.gz"}
con2 = sqlite3.connect(tmp / "b.db")
breadth.update(store2, con2, tmp / "work", log=lambda *_: None)
shutil.copy(tmp / "drive" / "astock_2020-07_2021-03.db.gz", tmp / "drive2")
store2.files["astock_2020-07_2021-03.db.gz"] = tmp / "drive2" / "astock_2020-07_2021-03.db.gz"
breadth.update(store2, con2, tmp / "work", log=lambda *_: None)
a = con.execute("select * from market_breadth order by 期間").fetchall()
b = con2.execute("select * from market_breadth order by 期間").fetchall()
assert a == b, [(x, y) for x, y in zip(a, b) if x != y][:2]
print(f"市場寬度測試通過：{len(a)} 天；最後一天 {a[-1][:6]}")

# 最新的包在計算途中被換成新檔名（行情流程同時在更新）：要能重新列檔案找到新檔，而且結果不變
store3 = LocalStore(tmp / "drive3")
for f in ("astock_2020-01_2020-06.db.gz", "astock_2020-07_2021-03.db.gz"):
    shutil.copy(tmp / "drive" / f, tmp / "drive3")
store3.files = {n: tmp / "drive3" / n for n in os.listdir(tmp / "drive3")}
os.rename(tmp / "drive3" / "astock_2020-07_2021-03.db.gz", tmp / "drive3" / "astock_2020-07_2021-04.db.gz")
con3 = sqlite3.connect(tmp / "c.db")
breadth.update(store3, con3, tmp / "work", log=lambda *_: None)
c3 = con3.execute("select * from market_breadth order by 期間").fetchall()
assert c3 == a, "換檔名後結果不同"
print("最新包中途換檔名：通過")
