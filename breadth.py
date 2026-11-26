# -*- coding: utf-8 -*-
"""
市場寬度（用自己的 A 股日線數據包算）：每日漲停／跌停家數、上漲下跌家數、站上 20／60／250 日線比例、
60／250 日新高新低家數、騰落線（ADL）、漲跌幅中位數、成交額
  範圍：滬深主板＋創業板（數據包本身就剔除科創板、北交所），只算當天有交易的股票
  漲跌停判斷：收盤價 ≥ 前收 ×（1＋漲跌幅限制）四捨五入到分；限制：主板 10%、ST 5%、創業板 2020-08-24 起 20%
    上市前 5 個交易日不設漲跌幅（新股）不算
  均線、新高新低用後復權收盤價；要有滿 N 天資料才算進分母
  結果寫進 macro.db 的 market_breadth 表；之後每天只算新的交易日（往前多讀約 15 個月的數據包當暖機）
"""
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from common import pack_get
from packing import existing

TABLE = "market_breadth"
KEEP = 260                       # 每檔股票跨包保留的天數（算 250 日線、250 日新高用）
CHINEXT_20 = "2020-08-24"        # 創業板改 20% 漲跌幅
STOCK_SQL = ("SELECT code, date, close, preclose, amount, tradestatus, is_st, adj_factor FROM daily "
             "WHERE (code LIKE 'sh.6%' OR code LIKE 'sz.00%' OR code LIKE 'sz.30%') ORDER BY code, date")


def limit_pct(code, d, is_st):
    """回傳每列的漲跌幅限制（向量化）"""
    cyb = code.str.startswith("sz.30")
    lim = np.where(is_st == 1, 0.05, 0.10)
    return np.where(cyb & (d >= CHINEXT_20), 0.20, lim)


def _round2(x):
    return np.floor(x * 100 + 0.5) / 100           # 四捨五入到分（避免銀行家捨入）


def compute(df):
    """df：多檔股票、依 code,date 排序（含前面暖機的天數）。回傳每列加上各指標的 DataFrame"""
    df = df.copy()
    df["adj_factor"] = df["adj_factor"].fillna(1.0)
    df["hfq"] = df["close"] * df["adj_factor"]
    g = df.groupby("code", sort=False)
    df["nth"] = g.cumcount()                       # 這檔在資料中的第幾天（0 起算）
    for n in (20, 60, 250):
        df[f"ma{n}"] = g["hfq"].transform(lambda s, n=n: s.rolling(n, min_periods=n).mean())
    for n in (60, 250):
        df[f"hi{n}"] = g["hfq"].transform(lambda s, n=n: s.rolling(n, min_periods=n).max())
        df[f"lo{n}"] = g["hfq"].transform(lambda s, n=n: s.rolling(n, min_periods=n).min())
    lim = limit_pct(df["code"], df["date"], df["is_st"].fillna(0))
    pc = df["preclose"]
    df["up_px"] = _round2(pc * (1 + lim))
    df["dn_px"] = _round2(pc * (1 - lim))
    df["chg"] = (df["close"] / pc - 1) * 100
    return df


def daily_stats(df):
    """每日彙總（df 已經只剩要輸出的日期、當天有交易的股票）"""
    eps = 1e-6
    new = df["nth"] >= 5                           # 新股前 5 天不算漲跌停
    df = df.assign(
        up=(df["chg"] > eps).astype(int), down=(df["chg"] < -eps).astype(int), flat=(df["chg"].abs() <= eps).astype(int),
        zt=((df["close"] >= df["up_px"] - 0.0049) & new).astype(int),
        dt=((df["close"] <= df["dn_px"] + 0.0049) & new).astype(int),
        up5=(df["chg"] >= 5).astype(int), dn5=(df["chg"] <= -5).astype(int),
    )
    for n in (20, 60, 250):
        ok = df[f"ma{n}"].notna()
        df[f"ab{n}"] = np.where(ok, (df["hfq"] > df[f"ma{n}"]).astype(float), np.nan)
    for n in (60, 250):
        ok = df[f"hi{n}"].notna()
        df[f"nh{n}"] = (ok & (df["hfq"] >= df[f"hi{n}"] - 1e-9)).astype(int)
        df[f"nl{n}"] = (ok & (df["hfq"] <= df[f"lo{n}"] + 1e-9)).astype(int)
    g = df.groupby("date")
    out = pd.DataFrame({
        "交易家数": g.size(),
        "上涨家数": g["up"].sum(), "下跌家数": g["down"].sum(), "平盘家数": g["flat"].sum(),
        "涨停家数": g["zt"].sum(), "跌停家数": g["dt"].sum(),
        "涨幅≥5%家数": g["up5"].sum(), "跌幅≥5%家数": g["dn5"].sum(),
        "涨跌幅中位数%": g["chg"].median().round(3), "等权平均涨跌幅%": g["chg"].mean().round(3),
        "成交额(亿元)": (g["amount"].sum() / 1e8).round(2),
        "站上20日线%": (g["ab20"].mean() * 100).round(2), "站上60日线%": (g["ab60"].mean() * 100).round(2),
        "站上250日线%": (g["ab250"].mean() * 100).round(2),
        "60日新高家数": g["nh60"].sum(), "60日新低家数": g["nl60"].sum(),
        "250日新高家数": g["nh250"].sum(), "250日新低家数": g["nl250"].sum(),
    })
    out["涨跌家数差"] = out["上涨家数"] - out["下跌家数"]
    return out.reset_index().rename(columns={"date": "期間"})


def _refresh(store):
    """重新列出 Drive 上的檔案（最新的數據包每天會換檔名）"""
    if hasattr(store, "_list"):
        store.files = store._list()
    elif hasattr(store, "root"):
        store.files = {p.name: p for p in Path(store.root).iterdir()}


def _download(store, lo, name, path):
    """下載數據包；失敗（例如同時有行情更新把最新包換掉）就重新列檔案、找涵蓋同一起月的新檔名再試一次"""
    try:
        if pack_get(store, name, path):
            return name
    except Exception:  # noqa: BLE001
        pass
    _refresh(store)
    for lo2, hi2, name2 in existing(store):
        if lo2 == lo and pack_get(store, name2, path):
            return name2
    raise RuntimeError(f"下載不到 {name}（重新列檔案後也找不到）")


def update(store, con, workdir, log=print, max_minutes=120):
    """從 Drive 上的數據包算市場寬度，寫進 con（macro.db）。回傳新增天數"""
    t0 = time.monotonic()
    packs = existing(store)
    if not packs:
        raise RuntimeError("Drive 上沒有 A 股數據包")
    has = con.execute("select 1 from sqlite_master where type='table' and name=?", (TABLE,)).fetchone()
    last, adl0 = None, 0
    if has:
        r = con.execute(f'select max("期間") from {TABLE}').fetchone()
        last = r[0]
        if last:
            adl0 = con.execute(f'select "腾落线ADL" from {TABLE} where "期間"=?', (last,)).fetchone()[0]
    if last:                     # 只需要最近的包：往前多讀約 15 個月暖機
        d = date.fromisoformat(last) - timedelta(days=460)
        need = d.year * 100 + d.month
        packs = [p for p in packs if p[1] >= need]
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    carry = None
    total = 0
    for lo, hi, name in packs:
        if (time.monotonic() - t0) / 60 > max_minutes:
            log(f"市場寬度：時間到，停在 {name} 之前（下次接著算）")
            break
        path = workdir / f"{name}.db"
        name = _download(store, lo, name, path)
        c = sqlite3.connect(str(path))
        df = pd.read_sql(STOCK_SQL, c)
        c.close()
        os.remove(path)
        pack_dates = set(df["date"])
        if carry is not None:
            df = pd.concat([carry, df], ignore_index=True).sort_values(["code", "date"], kind="stable")
        df = df.drop_duplicates(["code", "date"], keep="last").reset_index(drop=True)
        df = compute(df)
        out = df[df["date"].isin(pack_dates) & (df["tradestatus"] == 1) & df["preclose"].gt(0)]
        if last:
            out = out[out["date"] > last]
        if len(out):           # 每個包算完就寫進資料庫：中途出錯，已經算好的不會不見
            res = daily_stats(out).sort_values("期間")
            res["腾落线ADL"] = adl0 + res["涨跌家数差"].cumsum()
            adl0 = int(res["腾落线ADL"].iloc[-1])
            res.to_sql(TABLE, con, index=False, if_exists="append")
            con.commit()
            total += len(res)
            last = res["期間"].iloc[-1]
        base = ["code", "date", "close", "preclose", "amount", "tradestatus", "is_st", "adj_factor"]
        carry = df[base].groupby("code", sort=False).tail(KEEP)
        log(f"市場寬度：{name} 完成（{len(out)} 筆股票日）")
    return total
