# -*- coding: utf-8 -*-
"""
分包規則：每個數據包涵蓋「連續幾個月」，壓縮後盡量接近但不超過 250MB（Drive 上的檔案大小）
  檔名：astock_YYYY-MM_YYYY-MM.db.gz（起訖月份），內含 daily、m15、m60 三張表
  · 已經結束的包永遠不再變動
  · 最新的包會一直加入新月份（檔名的迄月跟著改），估計會超過目標就開新包
  · 每個月的大小用「當月上市檔數 × 交易日數 × 每列平均壓縮大小」估算，和資料抓到多少無關，所以分包結果穩定
"""
import os
import re
from datetime import date

from common import DAILY_START, M15_START, STOCK

LIMIT = float(os.environ.get("PACK_LIMIT_MB", "250")) * 1e6     # 每包上限（壓縮後，Drive 上的大小）
TARGET = LIMIT * 0.952                                           # 估算目標：估算誤差約 ±6%，留一點空間（250MB → 238MB）
# 每列平均壓縮大小：用 2026-11 的 21 個實際數據包回歸校準（誤差 ≤ 6%）；15 分鐘線的數字已包含對應的 60 分鐘線
BYTES_PER_ROW = {"daily": 59.0, "m15": 25.4, "m60": 0.0}
NAME_RE = re.compile(r"astock_(\d{4})-(\d{2})_(\d{4})-(\d{2})")


def ym_of(d):
    """'2025-03-14' → 202503"""
    return int(d[:4]) * 100 + int(d[5:7])


def ym_next(ym):
    y, m = divmod(ym, 100)
    return (y + 1) * 100 + 1 if m == 12 else ym + 1


def pack_name(lo, hi):
    return f"astock_{lo // 100}-{lo % 100:02d}_{hi // 100}-{hi % 100:02d}"


def parse(name):
    m = NAME_RE.fullmatch(name)
    if not m:
        return None
    a, b, c, d = map(int, m.groups())
    return a * 100 + b, c * 100 + d


def month_bounds(lo, hi, first=False):
    """包的範圍 → (日線起日, 日線迄日, 分鐘線 dt 下限, 上限)；第一個包的起日放寬，收進更早的零星資料"""
    d0 = "0000-00-00" if first else f"{lo // 100}-{lo % 100:02d}-01"
    d1 = f"{hi // 100}-{hi % 100:02d}-31"
    return d0, d1, lo * 1000000, hi * 1000000 + 999999


def existing(store):
    """Drive 上現有的區間包：[(lo, hi, name)]，依時間排序"""
    out = []
    for n in store.files:
        base = re.sub(r"\.db\.gz(\.part\d+)?$", "", n)
        p = parse(base)
        if p and (p[0], p[1], base) not in out:
            out.append((p[0], p[1], base))
    return sorted(out)


def month_estimates(meta, until=None):
    """每個月估計的壓縮後大小（bytes），從 DAILY_START 到 until（預設今天）"""
    until = until or date.today().isoformat()
    td = {}
    for d, ok in meta.execute("SELECT date, is_trading FROM trade_dates"):
        if ok:
            td[ym_of(d)] = td.get(ym_of(d), 0) + 1
    secs = meta.execute("SELECT kind, ipo_date, out_date FROM securities").fetchall()
    est = {}
    ym, end = ym_of(DAILY_START), ym_of(until)
    m15_from = ym_of(M15_START)
    while ym <= end:
        first = f"{ym // 100}-{ym % 100:02d}-01"
        last = f"{ym // 100}-{ym % 100:02d}-31"
        n_all = n_stock = 0
        for kind, ipo, out in secs:
            if (ipo or "0000") <= last and (not out or out >= first):
                n_all += 1
                n_stock += kind == STOCK
        days = td.get(ym, 21)
        b = n_all * days * BYTES_PER_ROW["daily"]
        if ym >= m15_from:
            b += n_stock * days * (16 * BYTES_PER_ROW["m15"] + 4 * BYTES_PER_ROW["m60"])
        est[ym] = b
        ym = ym_next(ym)
    return est


def greedy(months, est, start_size=0.0):
    """把連續的月份切成每組約 TARGET；回傳 [[月份...], ...]"""
    groups, cur, size = [], [], start_size
    for m in months:
        e = est.get(m, 0)
        if cur and size + e > TARGET:
            groups.append(cur)
            cur, size = [], 0.0
        cur.append(m)
        size += e
    if cur:
        groups.append(cur)
    return groups


def plan(ranges, months, est, sizes):
    """
    ranges：現有的包 [(lo, hi, name)]；months：這次要寫入的月份；est：每月估計大小；sizes：現有包的實際壓縮大小（bytes）
    回傳 (mapping {月份: 包名}, renames {新包名: 舊包名})
    """
    mapping, renames = {}, {}
    rest = []
    for m in sorted(set(months)):
        hit = next((n for lo, hi, n in ranges if lo <= m <= hi), None)
        if hit:
            mapping[m] = hit
        else:
            rest.append(m)
    if not rest:
        return mapping, renames
    last = ranges[-1] if ranges else None
    tail = [m for m in rest if not last or m > last[1]]
    gaps = [m for m in rest if last and m < last[1]]

    # 中間或更早的空缺（例如回補 2005–2014）：被現有包隔開的每一段各自切
    segs, cur = [], []
    for m in gaps:
        if cur and any(cur[-1] < lo <= m for lo, _, _ in ranges):
            segs.append(cur); cur = []
        cur.append(m)
    if cur:
        segs.append(cur)
    for seg in segs:
        for g in greedy(seg, est):
            name = pack_name(g[0], g[-1])
            for m in g:
                mapping[m] = name

    # 最新的月份：先看能不能接在最後一個包後面
    if tail:
        base = None
        if last:
            size = sizes.get(last[2])
            if size is None:
                ym, size = last[0], 0
                while ym <= last[1]:
                    size += est.get(ym, 0); ym = ym_next(ym)
            if size + est.get(tail[0], 0) <= TARGET:
                base = (last, size)
        groups = greedy(tail, est, base[1] if base else 0.0)
        for i, g in enumerate(groups):
            lo = base[0][0] if (i == 0 and base) else g[0]
            name = pack_name(lo, g[-1])
            if i == 0 and base and name != base[0][2]:
                renames[name] = base[0][2]
                for lo2, hi2, n2 in ranges:      # 原本落在舊包的月份也改指新名字
                    if n2 == base[0][2]:
                        for m, v in list(mapping.items()):
                            if v == n2:
                                mapping[m] = name
            for m in g:
                mapping[m] = name
    return mapping, renames
