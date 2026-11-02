#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地完整流程測試：假 BaoStock + 本地資料夾當 Drive，跑兩輪（補歷史、每日增量）"""
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import fake_baostock  # noqa: E402

sys.modules["baostock"] = fake_baostock

tmp = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(tmp / "data")
os.environ["LOCAL_STORE"] = str(tmp / "drive")
os.environ["SHARDS"] = "4"

import fetch  # noqa: E402
import merge  # noqa: E402
import prepare  # noqa: E402
from common import DATA, gunzip_file, pack_get  # noqa: E402
from store import LocalStore  # noqa: E402


def one_round(label):
    if DATA.exists():
        shutil.rmtree(DATA)
    prepare.main()
    for s in range(4):
        sys.argv = ["fetch.py", str(s)]
        fetch.main()
    (DATA / "deltas").mkdir()
    for f in glob.glob(str(DATA / "delta_*.db")):
        d = DATA / "deltas" / f"delta-{Path(f).stem[-1]}"
        d.mkdir()
        shutil.move(f, d)
    merge.main()
    print(f"===== {label} 完成 =====")


def open_pack(month):
    """打開涵蓋這個月份（'YYYY-MM'）的數據包"""
    from packing import existing
    ym = int(month[:4]) * 100 + int(month[5:7])
    st = LocalStore(tmp / "drive")
    name = next(n for lo, hi, n in existing(st) if lo <= ym <= hi)
    out = tmp / f"{name}.db"
    if not out.exists():
        assert pack_get(st, name, out), name
    return sqlite3.connect(out)


one_round("第一輪：補歷史")
drive = sorted(os.listdir(tmp / "drive"))
print("Drive 檔案數:", len(drive), drive[:5], "...", drive[-5:])

# 第二輪：資料源多了兩個交易日
fake_baostock.TODAY = date(2026, 11, 3)
fake_baostock.CALLS.clear()
one_round("第二輪：每日增量")
calls = fake_baostock.CALLS
old = [c for c in calls if c[3] < "2026-10-31" and not (c[0] == "sh.600000" and c[3] >= "2026-10-01")]
assert not old, old[:5]
print("第二輪查詢次數:", len(calls), "（只抓新日期）")

# 驗證
meta = sqlite3.connect(tmp / "meta.db")
gunzip_file(tmp / "drive" / "meta.db.gz", tmp / "meta.db")
meta = sqlite3.connect(tmp / "meta.db")
codes = [r[0] for r in meta.execute("SELECT code FROM securities ORDER BY code")]
print("股票清單:", codes)
assert "sh.688981" not in codes and "bj.830799" not in codes and "sh.600002" in codes   # 2010 退市，2005 起算後要有
assert "sh.113001" not in codes
prog = meta.execute("SELECT * FROM progress ORDER BY code, dataset").fetchall()
for p in prog:
    print("  進度", p)
assert ("sh.600001", "daily", "2021-03-15") in prog, "退市股應停在退市日"
assert ("sh.600002", "daily", "2010-03-15") in prog and ("sh.600002", "daily_old", "2010-03-15") in prog
assert ("sh.600000", "daily", "2026-11-03") in prog
assert ("sh.600000", "m15", "2026-11-03") in prog
assert not any(p[0] in ("sh.510300", "sh.000300") and p[1] == "m15" for p in prog)

c = open_pack("2026-11")
n, f = c.execute("SELECT COUNT(*), MIN(adj_factor) FROM daily WHERE code='sh.600000'").fetchone()
assert abs(f - 1.5) < 1e-6, f
dup = c.execute("SELECT COUNT(*) - COUNT(DISTINCT code||date) FROM daily").fetchone()[0]
assert dup == 0
c = open_pack("2026-11")
kinds = c.execute("SELECT code, COUNT(*), MIN(adj_factor), MAX(pe_ttm) FROM daily GROUP BY code").fetchall()
print("基金/指數包:", kinds)
assert {"sh.510300", "sz.159915", "sh.000300"} <= {k[0] for k in kinds}

c = open_pack("2026-11")
print("m15 2026-11:", c.execute("SELECT code, COUNT(*), MIN(dt), MAX(dt) FROM m15 GROUP BY code").fetchall())
c = open_pack("2026-11")
rows = c.execute("SELECT * FROM m60 WHERE code='sh.600000' AND dt/10000=20261103 ORDER BY dt").fetchall()
print("60分 2026-11-03:", rows)
p = fake_baostock.price("sh.600000", date(2026, 11, 3))
assert [r[1] % 10000 for r in rows] == [1030, 1130, 1400, 1500]
assert rows[0][2] == p + 0 and rows[0][5] == p + 3 + 1, rows[0]      # 開=第1根開，收=第4根收
assert rows[0][3] == p + 3 + 2 and rows[0][4] == p - 1               # 高、低
assert rows[0][6] == 100 + 200 + 300 + 400                           # 量相加
assert rows[3][2] == p + 12 and rows[3][5] == p + 16

packs = meta.execute("SELECT name, rows, size_mb FROM packs ORDER BY name").fetchall()
print("數據包:", len(packs), packs[:3])
print(meta.execute("SELECT * FROM runs").fetchall()[-1])
print("\n全部測試通過 ✅")
