#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/analyze.py 本地測試：用假資料建出 meta.db 與數據包（含分片、.gz、.db 三種形式），跑完整報告"""
import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import analyze  # noqa: E402
from common import DAILY_COLS, META_SCHEMA, SCHEMA  # noqa: E402

rng = np.random.default_rng(7)
tmp = Path(tempfile.mkdtemp())
days = pd.bdate_range("2019-01-02", "2026-11-13")
D = [d.strftime("%Y-%m-%d") for d in days]
N = len(D)

IND_A, IND_B = "C27医药制造业", "C39计算机、通信和其他电子设备制造业"
secs = [("sh.600001", "测试甲", "stock", IND_A, 0.0006), ("sz.000002", "测试乙", "stock", IND_A, 0.0002),
        ("sz.300003", "测试丙", "stock", IND_A, 0.0004), ("sh.600004", "测试丁", "stock", IND_B, -0.0002)]
secs += [(f"sz.00{1000 + i}", f"填充{i}", "stock", IND_B, rng.normal(0.0002, 0.0004)) for i in range(40)]
ETF, IDX = ("sh.510300", "沪深300ETF", "etf", None, 0.0003), ("sh.000300", "沪深300", "index", None, 0.0003)


def series(drift, vol=0.02):
    lr = rng.normal(drift, vol, N)
    lr[-120:] += 0.002 if drift > 0.0005 else 0          # 主角最近走強
    return 20 * np.exp(np.cumsum(lr))


rows = []
for code, name, kind, ind, drift in secs + [ETF, IDX]:
    ph = series(drift)                                   # 後復權價
    adj = np.where(np.array(D) >= "2023-07-10", 1.1, 1.0)  # 2023-07-10 除權：不復權價下跌 1/1.1
    adj = np.where(np.array(D) >= "2025-06-20", adj * 1.05, adj)
    raw = ph / adj
    eh = 0.8 * np.exp(np.linspace(0, 0.14 * 7.9, N))      # EPS 每年約 +15%（後復權尺度）
    bh = 6 * np.exp(np.linspace(0, 0.08 * 7.9, N))
    sh_ = 9 * np.exp(np.linspace(0, 0.10 * 7.9, N))
    status = np.ones(N, int)
    if code == "sz.000002":
        status[-170:-160] = 0
    for i in range(N):
        o = raw[i] * (1 + rng.normal(0, 0.005))
        h_, l_ = max(o, raw[i]) * (1 + abs(rng.normal(0, 0.008))), min(o, raw[i]) * (1 - abs(rng.normal(0, 0.008)))
        vol = 0 if status[i] == 0 else float(rng.integers(5e5, 3e6))
        pc = raw[i - 1] if i else raw[i]
        val = kind == "stock"
        rows.append((code, D[i], o, h_, l_, raw[i], pc, vol, vol * raw[i], vol / 1e7 * 100, int(status[i]),
                     (raw[i] / pc - 1) * 100, ph[i] / eh[i] if val else None, ph[i] / bh[i] if val else None,
                     ph[i] / sh_[i] if val else None, ph[i] / eh[i] * 0.8 if val else None, 0, adj[i]))
daily = pd.DataFrame(rows, columns=DAILY_COLS)


def pack(name, lo, hi, form):
    db = tmp / f"{name}.db"
    con = sqlite3.connect(db)
    con.execute(SCHEMA["daily"])
    sub = daily[(daily.date >= lo) & (daily.date <= hi)]
    con.executemany(f"INSERT INTO daily VALUES({','.join('?' * len(DAILY_COLS))})", sub.itertuples(index=False))
    con.commit()
    con.close()
    if form == "db":
        return
    gz = tmp / f"{name}.db.gz"
    with open(db, "rb") as a, gzip.open(gz, "wb") as b:
        shutil.copyfileobj(a, b)
    db.unlink()
    if form == "parts":
        data = gz.read_bytes()
        cut = len(data) // 2
        (tmp / f"{name}.db.gz.part01").write_bytes(data[:cut])
        (tmp / f"{name}.db.gz.part02").write_bytes(data[cut:])
        gz.unlink()


pack("astock_2019-01_2022-12", "2019-01-01", "2022-12-31", "parts")
pack("astock_2023-01_2025-12", "2023-01-01", "2025-12-31", "db")
pack("astock_2026-01_2026-11", "2026-01-01", "2026-11-30", "gz")

meta = sqlite3.connect(tmp / "meta.db")
for ddl in META_SCHEMA:
    meta.execute(ddl)
for code, name, kind, ind, _ in secs + [ETF, IDX]:
    meta.execute("INSERT INTO securities VALUES(?,?,?,?,?,?,?)", (code, name, kind, 1, "2010-01-01", None, None))
    if ind:
        meta.execute("INSERT INTO industry VALUES(?,?,?,?)", (code, ind, "证监会行业分类", "2026-11-01"))
meta.execute("INSERT INTO index_members VALUES('沪深300','sh.600001','测试甲','2026-11-01')")
meta.commit()
meta.close()
with open(tmp / "meta.db", "rb") as a, gzip.open(tmp / "meta.db.gz", "wb") as b:
    shutil.copyfileobj(a, b)
(tmp / "meta.db").unlink()

# 模擬 Drive：所有數據包都是 .db.gz（或分片），給 analyze_ci.py 用
drive = Path(tempfile.mkdtemp())
for f in tmp.iterdir():
    if f.suffix == ".db":
        with open(f, "rb") as a, gzip.open(drive / (f.name + ".gz"), "wb") as b:
            shutil.copyfileobj(a, b)
    else:
        shutil.copy(f, drive / f.name)

# ---------------- 指標單元測試
up = pd.Series(np.arange(1, 60, dtype=float))
assert analyze.rsi(up).iloc[-1] == 100
k, d = analyze.kd(up + 1, up - 1, up)
assert 0 <= k.min() and k.max() <= 100 and 0 <= d.min() and d.max() <= 100
flat = pd.Series(np.full(50, 10.0))
assert abs(analyze.atr(flat + 1, flat - 1, flat).iloc[-1] - 2) < 1e-9
dif, dea, bar = analyze.macd(up)
assert dif.iloc[-1] > 0 and abs(bar.iloc[-1] - 2 * (dif.iloc[-1] - dea.iloc[-1])) < 1e-12

# ---------------- 完整報告
out = tmp / "r1.md"
r = analyze.main(["600001", "--dir", str(tmp), "--capital", "1000000", "--out", str(out)])
md = out.read_text(encoding="utf-8")
for sec in ("## 結論", "## 第一步", "## 第二步", "## 第三步", "## 第四步", "## 第五步", "## 第六步", "## 第七步", "## 總評分表"):
    assert sec in md, sec
assert r["asof"] == "2026-11-13" and r["start"] <= "2021-11-15", (r["asof"], r["start"])
assert (tmp / "astock_2019-01_2022-12.db").exists()                # 分片接起來解壓成功
assert (tmp / "astock_2026-01_2026-11.db").exists()                # .gz 已自動解壓
assert r["industry"] == IND_A and r["peer_n"] == 3 and r["breadth"] is not None
# 前復權後 EPS 不應該在除權日跳動（真實 EPS 每年 +15%）
assert 0.10 < r["eps_g"] < 0.20, r["eps_g"]
assert 0.10 < r["eps_cagr3"] < 0.20, r["eps_cagr3"]
assert r["pe_pct"] is not None and r["fair"] and r["fair"][0] < r["fair"][1] < r["fair"][2]
p = r["plan"]
assert p["stop"] < p["entry"] < p["t1"] and p["shares"] % 100 == 0 and p["shares"] * p["entry"] <= 200_000 + 1e-6
assert all(s < r["close"] for s, _ in r["support"]) and all(x > r["close"] for x, _ in r["resist"])
assert "尚未完成" in md or "一票否決" in md

# 補上人工評分 → 有完整決策；用名稱查、指定歷史日期、跳過全市場掃描
r2 = analyze.main(["测试甲", "--dir", str(tmp), "--date", "2024-06-28", "--years", "3", "--fast",
                   "--chips", "4", "--catalyst", "3", "--out", str(tmp / "r2.md")])
md2 = (tmp / "r2.md").read_text(encoding="utf-8")
assert r2["asof"] == "2024-06-28" and "尚未完成" not in md2

# 停牌過的股票、ETF、各種代碼寫法
r3 = analyze.main(["000002.SZ", "--dir", str(tmp), "--fast", "--out", str(tmp / "r3.md")])
assert r3["code"] == "sz.000002" and any("停牌" in f for f in r3["flags"])
r4 = analyze.main(["SH510300", "--dir", str(tmp), "--out", str(tmp / "r4.md")])
assert r4["kind"] == "etf" and not r4["has_val"]
assert analyze.resolve(analyze.Data(tmp), "sh.600004")[1] == "测试丁"

# ---------------- GitHub Actions 流程：從（模擬的）Drive 下載、精簡、分析
import os  # noqa: E402
work = Path(tempfile.mkdtemp())
os.environ.update(LOCAL_STORE=str(drive), ANALYZE_DIR=str(work / "data"), CODES="600001, 测试丙", YEARS="3")
os.chdir(work)
import analyze_ci  # noqa: E402
analyze_ci.main()
reports = sorted(p.name for p in (work / "reports").iterdir())
assert reports == ["sh600001_2026-11-13.md", "sz300003_2026-11-13.md"], reports
slim = sorted(p.name for p in (work / "data").iterdir())
# 回看 3 年只需要 2023-10 之後：最舊的包不下載，大檔用完就刪
assert slim == ["astock_2023-01_2025-12.db", "astock_2026-01_2026-11.db", "meta.db"], slim
con = sqlite3.connect(work / "data" / "astock_2023-01_2025-12.db")
assert {x[0] for x in con.execute("SELECT DISTINCT code FROM daily")} == {"sh.600001", "sz.300003", "sh.000300"}
ci_md = (work / "reports" / "sh600001_2026-11-13.md").read_text(encoding="utf-8")
assert "站上季線的個股比例" in ci_md and "同產業成交額前幾大" in ci_md

print("\n===== analyze 測試全部通過 =====", file=sys.stderr)
for d in (tmp, drive, work):
    shutil.rmtree(d)
