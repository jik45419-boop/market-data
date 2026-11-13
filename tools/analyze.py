#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""個股分析報告：從 market-data 資料庫自動算出「買股前個股分析清單」能量化的部分

用法（先把 Drive 上的 market-data 資料夾下載到電腦）：
  python tools/analyze.py 600519 --dir D:/market-data
  python tools/analyze.py 贵州茅台 --dir ~/market-data --capital 1000000 --risk 0.01
  python tools/analyze.py sz.000858 --dir ~/market-data --date 2025-06-30     # 把歷史某一天當作「今天」
  python tools/analyze.py 600519 --dir ~/market-data --chips 4 --catalyst 3   # 補上人工評分，算出總分與決策

--dir 放 meta.db(.gz) 與 astock_*.db(.gz)；只會解壓需要的包（每包解壓後約 800MB，解壓後留著下次直接用）。
預設回看 5 年（估值百分位、EPS 趨勢用）；硬碟不夠可用 --years 3。
報告存成 reports/<代碼>_<日期>.md，同時印在畫面上。需要 pandas、numpy。

注意：
- 資料庫只有行情與估值（PE/PB/PS），所以基本面是「由估值反推」的估算值（EPS = 股價 ÷ PE、ROE ≈ PB ÷ PE），
  正式判斷仍要看財報；籌碼（融資融券、北向資金、股東人數）與催化劑不在資料庫裡，要人工補分。
- 這是分析工具，不是投資建議。
"""
import argparse
import gzip
import math
import re
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HS300 = "sh.000300"
INDEX_NAMES = {"hs300": "滬深300", "sz50": "上證50", "zz500": "中證500"}
PACK_RE = re.compile(r"^(astock_\d{4}-\d{2}_\d{4}-\d{2}|astock_\d{4})\.db(?:\.gz(?:\.part\d+)?)?$")

# 與分析清單「總評分表」相同的面向與權重
SECTIONS = [
    ("env", "第一步 總體與產業", 0.10),
    ("fund", "第二步 基本面", 0.25),
    ("val", "第三步 估值", 0.20),
    ("chips", "第四步 籌碼面", 0.15),
    ("tech", "第五步 技術面", 0.10),
    ("cat", "第六步 催化劑與風險", 0.10),
    ("plan", "第七步 交易計畫", 0.10),
]


# ====================================================================== 資料讀取
class _Chain:
    """把 .part01、.part02… 依序接成一個可讀的串流"""

    def __init__(self, files):
        self.files, self.f = list(files), None

    def read(self, n=-1):
        while True:
            if self.f is None:
                if not self.files:
                    return b""
                self.f = open(self.files.pop(0), "rb")
            b = self.f.read(n)
            if b:
                return b
            self.f.close()
            self.f = None


def _span(name):
    """數據包名稱 → 涵蓋的 (起 YYYYMM, 迄 YYYYMM)"""
    m = re.match(r"astock_(\d{4})-(\d{2})_(\d{4})-(\d{2})$", name)
    if m:
        a, b, c, d = map(int, m.groups())
        return a * 100 + b, c * 100 + d
    y = int(name[7:11])
    return y * 100 + 1, y * 100 + 12


def _ym(d):
    return int(d[:4]) * 100 + int(d[5:7])


class Data:
    def __init__(self, folder):
        self.dir = Path(folder).expanduser().resolve()
        if not self.dir.is_dir():
            sys.exit(f"找不到資料夾：{self.dir}")
        names = {PACK_RE.match(p.name).group(1) for p in self.dir.iterdir() if PACK_RE.match(p.name)}
        # 依迄月排序；同一段日期出現在兩個包（最新包改名）時，後面的包優先
        self.packs = sorted(((*_span(n), n) for n in names), key=lambda x: (x[1], x[0]))
        if not self.packs:
            sys.exit(f"{self.dir} 裡沒有 astock_*.db 或 astock_*.db.gz")
        meta = self._ensure("meta")
        if meta is None:
            sys.exit(f"{self.dir} 裡沒有 meta.db 或 meta.db.gz")
        self.meta = self._connect(meta)
        self.missing = []

    @staticmethod
    def _connect(path):
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)

    def _ensure(self, name):
        """回傳可查詢的 .db；只有 .db.gz（或分片）時先解壓。.gz 比 .db 新（最新包每天更新）就重新解壓"""
        db = self.dir / f"{name}.db"
        gz = sorted(self.dir.glob(f"{name}.db.gz.part*")) or (
            [self.dir / f"{name}.db.gz"] if (self.dir / f"{name}.db.gz").exists() else [])
        if db.exists() and (not gz or db.stat().st_mtime >= max(p.stat().st_mtime for p in gz)):
            return db
        if not gz:
            return None
        print(f"解壓 {gz[0].name}{'（' + str(len(gz)) + ' 個分片）' if len(gz) > 1 else ''} …", file=sys.stderr, flush=True)
        tmp = db.with_suffix(".db.tmp")
        with gzip.GzipFile(fileobj=_Chain(gz)) as g, open(tmp, "wb") as o:
            shutil.copyfileobj(g, o, 8 * 1024 * 1024)
        tmp.replace(db)
        return db

    def daily(self, codes, start, end, cols="*"):
        """日線；codes=None 表示全市場（會掃整個包，較慢）"""
        frames = []
        for lo, hi, name in self.packs:
            if hi < _ym(start) or lo > _ym(end):
                continue
            db = self._ensure(name)
            if db is None:
                continue
            con = self._connect(db)
            try:
                if codes is None:
                    q, p = f"SELECT {cols} FROM daily WHERE date BETWEEN ? AND ?", [start, end]
                else:
                    q = (f"SELECT {cols} FROM daily WHERE code IN ({','.join('?' * len(codes))}) "
                         "AND date BETWEEN ? AND ?")
                    p = [*codes, start, end]
                frames.append(pd.read_sql_query(q, con, params=p))
            finally:
                con.close()
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates(["code", "date"], keep="last").sort_values(["code", "date"]).reset_index(drop=True)

    def latest_date(self):
        lo, hi, name = self.packs[-1]
        con = self._connect(self._ensure(name))
        try:
            return con.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        finally:
            con.close()


def resolve(data, text):
    """600519 / sh.600519 / 600519.SH / SH600519 / 名稱 → (code, name, kind)"""
    t = text.strip()
    m = re.fullmatch(r"(?i)(sh|sz)\.?(\d{6})", t) or re.fullmatch(r"(?i)(\d{6})\.(sh|sz)", t)
    cands = []
    if m:
        a, b = m.groups()
        cands = [f"{a.lower()}.{b}"] if a.isalpha() else [f"{b.lower()}.{a}"]
    elif re.fullmatch(r"\d{6}", t):
        cands = [f"sh.{t}", f"sz.{t}"] if t[0] in "56" else [f"sz.{t}", f"sh.{t}"]
    q = data.meta.execute
    for c in cands:   # 同一個數字可能同時是股票與指數（如 000001），股票優先
        rows = q("SELECT code, name, kind FROM securities WHERE code=?", (c,)).fetchall()
        if rows:
            return rows[0]
    rows = q("SELECT code, name, kind FROM securities WHERE name=? ORDER BY kind='stock' DESC", (t,)).fetchall() \
        or q("SELECT code, name, kind FROM securities WHERE name LIKE ? ORDER BY kind='stock' DESC, status DESC",
             (f"%{t}%",)).fetchall()
    if len(rows) > 1 and rows[0][2] == rows[1][2]:
        sys.exit("名稱有多個符合，請改用代碼：" + "、".join(f"{c} {n}" for c, n, _ in rows[:10]))
    if rows:
        return rows[0]
    sys.exit(f"meta.db 裡找不到「{text}」")


# ====================================================================== 指標
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn)


def macd(c):
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    return dif, dea, 2 * (dif - dea)


def kd(h, l, c, n=9):
    llv, hhv = l.rolling(n).min(), h.rolling(n).max()
    rsv = ((c - llv) / (hhv - llv) * 100).fillna(50)
    k, d, kp, dp = [], [], 50.0, 50.0
    for v in rsv:
        kp = kp * 2 / 3 + v / 3
        dp = dp * 2 / 3 + kp / 3
        k.append(kp)
        d.append(dp)
    return pd.Series(k, c.index), pd.Series(d, c.index)


def atr(h, l, c, n=14):
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return wilder(tr, n)


def pivots(s, k=5, find="high"):
    """左右各 k 根內的最高（低）點；最後 k 根還沒確認，不算"""
    w = s.rolling(2 * k + 1, center=True)
    ext = w.max() if find == "high" else w.min()
    return list(s.index[(s == ext) & ext.notna()])


def divergence(price, osc, piv, find):
    """最近兩個轉折：價格創新高（低）但指標沒有 → 頂（底）背離"""
    piv = [i for i in piv if i >= len(price) - 120]
    if len(piv) < 2:
        return False
    a, b = piv[-2], piv[-1]
    if find == "high":
        return price.iloc[b] > price.iloc[a] and osc.iloc[b] < osc.iloc[a]
    return price.iloc[b] < price.iloc[a] and osc.iloc[b] > osc.iloc[a]


def volume_nodes(df, n=120, bins=24):
    """最近 n 天成交金額最集中的價位（籌碼密集區）"""
    s = df.tail(n)
    tp = (s.high + s.low + s.close) / 3
    lo, hi = s.low.min(), s.high.max()
    if not hi > lo:
        return []
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    w = pd.Series(s.amount.values, index=idx).groupby(level=0).sum()
    top = w.sort_values(ascending=False).head(3).index
    return [(edges[i] + edges[i + 1]) / 2 for i in top]


def levels(close, cands, atr_now, above):
    """從候選價位挑出最近的兩個支撐（或壓力），彼此至少相距 0.75 ATR 且 1.5%"""
    gap = max(atr_now * 0.75, close * 0.015)
    xs = sorted(((p, why) for p, why in cands if p and not math.isnan(p) and
                 (p > close + gap * 0.5 if above else p < close - gap * 0.5)),
                key=lambda x: x[0] if above else -x[0])
    out = []
    for p, why in xs:
        if out and abs(p - out[-1][0]) < gap:
            if why not in out[-1][1]:
                out[-1] = (out[-1][0], out[-1][1] + "、" + why)
            continue
        out.append((p, why))
        if len(out) == 2:
            break
    return out


def at_offset(s, dates, asof, days):
    """asof 往前 days 天（日曆日）那天或之前最近的值"""
    t = (date.fromisoformat(asof) - timedelta(days=days)).isoformat()
    m = dates <= t
    if not m.any() or dates[m].iloc[-1] < (date.fromisoformat(t) - timedelta(days=20)).isoformat():
        return None
    v = s[m].iloc[-1]
    return None if pd.isna(v) else float(v)


def ret(c, n):
    return float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None


def score(points, full):
    return 1 + round(4 * points / full)


# ====================================================================== 分析
def analyze(data, code, name, kind, asof_arg, years, capital, risk, entry_arg, fast):
    asof_guess = asof_arg or data.latest_date()
    start = (date.fromisoformat(asof_guess) - timedelta(days=int(365.25 * years) + 30)).isoformat()
    raw = data.daily([code, HS300], start, asof_guess)
    if raw.empty or (raw.code == code).sum() == 0:
        sys.exit(f"{code} 在 {start}～{asof_guess} 沒有資料（數據包是否都在資料夾裡？）")
    for c in raw.columns[2:]:
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    allrows = raw[raw.code == code].reset_index(drop=True)
    idx = raw[raw.code == HS300].reset_index(drop=True)
    df = allrows[(allrows.tradestatus == 1) & (allrows.volume > 0)].reset_index(drop=True)
    if len(df) < 60:
        sys.exit(f"{code} 交易日太少（{len(df)} 天），無法分析")
    asof = df.date.iloc[-1]
    r = {"code": code, "name": name, "kind": kind, "asof": asof, "start": df.date.iloc[0], "years": years}
    want_start = (date.fromisoformat(asof) - timedelta(days=int(365.25 * years))).isoformat()
    r["short_history"] = df.date.iloc[0] > (date.fromisoformat(want_start) + timedelta(days=45)).isoformat()

    # 前復權（以 asof 當天為基準，價格和今天的股價同一個尺度）
    adj = df.adj_factor.fillna(1.0).replace(0, 1.0)
    f = adj / adj.iloc[-1]
    for c in ("open", "high", "low", "close"):
        df[c + "_raw"] = df[c]
        df[c] = df[c] * f
    c, h, l, v = df.close, df.high, df.low, df.volume
    close = float(c.iloc[-1])
    r["close"], r["close_raw"] = close, float(df.close_raw.iloc[-1])
    r["pct_chg"] = float(df.pct_chg.iloc[-1]) if pd.notna(df.pct_chg.iloc[-1]) else None

    # ---------------- 技術面
    for n in (5, 10, 20, 60, 120, 250):
        r[f"ma{n}"] = float(c.rolling(n).mean().iloc[-1]) if len(c) >= n else None
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    r["ma20_up"] = bool(ma20.iloc[-1] > ma20.iloc[-6])
    r["ma60_up"] = bool(ma60.iloc[-1] > ma60.iloc[-21]) if len(c) >= 81 else None
    r["bull_align"] = bool(r["ma250"] and r["ma20"] > r["ma60"] > r["ma250"])
    r["bear_align"] = bool(r["ma250"] and r["ma20"] < r["ma60"] < r["ma250"])

    wk = df.assign(d=pd.to_datetime(df.date)).set_index("d").resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    wma20 = wk.close.rolling(20).mean()
    r["w_above_ma20"] = bool(wk.close.iloc[-1] > wma20.iloc[-1]) if len(wk) >= 20 else None
    r["w_ma20_up"] = bool(wma20.iloc[-1] > wma20.iloc[-5]) if len(wk) >= 24 else None
    wdif, wdea, _ = macd(wk.close)
    r["w_macd_bull"] = bool(wdif.iloc[-1] > wdea.iloc[-1]) if len(wk) >= 35 else None

    rs = rsi(c)
    dif, dea, bar = macd(c)
    k, d = kd(h, l, c)
    a = atr(h, l, c)
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    up_b, dn_b = mid + 2 * sd, mid - 2 * sd
    r.update(rsi=float(rs.iloc[-1]), dif=float(dif.iloc[-1]), dea=float(dea.iloc[-1]), bar=float(bar.iloc[-1]),
             bar_prev=float(bar.iloc[-2]), k=float(k.iloc[-1]), d=float(d.iloc[-1]),
             k_prev=float(k.iloc[-2]), d_prev=float(d.iloc[-2]), atr=float(a.iloc[-1]),
             boll_up=float(up_b.iloc[-1]), boll_mid=float(mid.iloc[-1]), boll_dn=float(dn_b.iloc[-1]),
             boll_pctb=float((close - dn_b.iloc[-1]) / (up_b.iloc[-1] - dn_b.iloc[-1]))
             if up_b.iloc[-1] > dn_b.iloc[-1] else None,
             boll_width=float((up_b.iloc[-1] - dn_b.iloc[-1]) / mid.iloc[-1]),
             boll_width_pct=float((((up_b - dn_b) / mid).tail(250) < (up_b.iloc[-1] - dn_b.iloc[-1]) / mid.iloc[-1]).mean()))
    r["atr_pct"] = r["atr"] / close
    r["bias20"] = close / r["ma20"] - 1

    sh, sl = pivots(h, 10, "high"), pivots(l, 10, "low")             # 波段轉折：支撐壓力、趨勢結構、背離
    r["top_div_rsi"] = divergence(h, rs, sh, "high")
    r["top_div_macd"] = divergence(h, dif, sh, "high")
    r["bot_div_rsi"] = divergence(l, rs, sl, "low")
    r["bot_div_macd"] = divergence(l, dif, sl, "low")
    rh, rl = [i for i in sh if i >= len(c) - 160], [i for i in sl if i >= len(c) - 160]
    r["hh_hl"] = len(rh) >= 2 and len(rl) >= 2 and h.iloc[rh[-1]] > h.iloc[rh[-2]] and l.iloc[rl[-1]] > l.iloc[rl[-2]]
    r["lh_ll"] = len(rh) >= 2 and len(rl) >= 2 and h.iloc[rh[-1]] < h.iloc[rh[-2]] and l.iloc[rl[-1]] < l.iloc[rl[-2]]

    n250 = min(len(c), 250)
    r["hi52"], r["lo52"] = float(h.tail(n250).max()), float(l.tail(n250).min())
    r["from_hi52"] = close / r["hi52"] - 1
    r["from_lo52"] = close / r["lo52"] - 1

    vol5, vol20 = v.tail(5).mean(), v.tail(20).mean()
    r["vol_ratio"] = float(vol5 / vol20) if vol20 else None
    last20 = df.tail(20)
    upv, dnv = last20.volume[last20.close.diff() > 0].sum(), last20.volume[last20.close.diff() < 0].sum()
    r["updown_vol"] = float(upv / dnv) if dnv else None
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    r["obv_up"] = bool(obv.iloc[-1] > obv.rolling(20).mean().iloc[-1])
    r["turn5"] = float(df.turn.tail(5).mean()) if df.turn.notna().any() else None
    r["turn60"] = float(df.turn.tail(60).mean()) if df.turn.notna().any() else None
    r["amount20"] = float(df.amount.tail(20).mean())
    is_gem = code.startswith(("sz.300", "sz.301"))
    sec = data.meta.execute("SELECT ipo_date FROM securities WHERE code=?", (code,)).fetchone()
    r["ipo"] = sec[0] if sec else None
    lim = 19.8 if is_gem and asof >= "2020-08-24" else 9.8
    pc20 = df.pct_chg.tail(20)
    r["limit_up20"] = int((pc20 >= lim).sum())
    r["limit_dn20"] = int((pc20 <= -lim).sum())

    cands = [(h.iloc[i], "前高") for i in sh if i >= len(c) - 250] + \
            [(l.iloc[i], "前低") for i in sl if i >= len(c) - 250] + \
            [(r["ma20"], "月線"), (r["ma60"], "季線"), (r["ma250"], "年線"), (r["hi52"], "52 週高"),
             (r["lo52"], "52 週低")] + [(p, "成交密集區") for p in volume_nodes(df)]
    r["support"] = levels(close, cands, r["atr"], above=False)
    r["resist"] = levels(close, cands, r["atr"], above=True)

    # 報酬、波動、相對大盤
    for key, n in (("r1w", 5), ("r1m", 21), ("r3m", 63), ("r6m", 126), ("r1y", 250)):
        r[key] = ret(c, n)
    # 資料品質：單日漲跌超過漲跌停的資料點多半是資料錯誤，列出來並排除在波動與 Beta 之外
    dq = []
    lim_row = pd.Series(np.where(is_gem & (df.date >= "2020-08-24"), 0.21, 0.11), index=df.index)
    if r["ipo"]:
        lim_row[df.date <= (date.fromisoformat(r["ipo"]) + timedelta(days=10)).isoformat()] = np.inf
    dr = c.pct_change()
    bad = dr.abs() > lim_row
    for i in list(df.index[bad])[-5:]:
        dq.append(f"{r['code']} {df.date[i]}：前復權收盤 {c[i - 1]:.2f} → {c[i]:.2f}（{dr[i]:+.1%}），"
                  f"復權因子 {df.adj_factor[i - 1]} → {df.adj_factor[i]}")
    if bad.sum() > 5:
        dq.append(f"{r['code']} 另有 {int(bad.sum()) - 5} 個異常日")
    n_adj_na = int(df.adj_factor.isna().sum())
    if n_adj_na:
        dq.append(f"{r['code']} 有 {n_adj_na} 天缺少復權因子（當作 1 處理，前復權價可能跳動）")
    lr = np.log(c).diff().mask(bad)
    r["vol60"] = float(lr.tail(60).std() * math.sqrt(250))
    cy = c.tail(250)
    r["mdd1y"] = float((cy / cy.cummax() - 1).min())
    if len(idx) > 60:
        ic = idx.set_index("date").close
        ir = ic.pct_change()
        ibad = ir.abs() > 0.11
        for d_ in list(ic.index[ibad])[-5:]:
            k_ = ic.index.get_loc(d_)
            dq.append(f"滬深300 {d_}：收盤 {ic.iloc[k_ - 1]:.2f} → {ic.iloc[k_]:.2f}（{ir.iloc[k_]:+.1%}）")
        if ibad.sum() > 5:
            dq.append(f"滬深300 另有 {int(ibad.sum()) - 5} 個異常日")
        r["idx_close"] = float(ic.iloc[-1])
        r["idx_ma60"] = float(ic.rolling(60).mean().iloc[-1])
        r["idx_ma250"] = float(ic.rolling(250).mean().iloc[-1]) if len(ic) >= 250 else None
        r["idx_ma60_up"] = bool(ic.rolling(60).mean().iloc[-1] > ic.rolling(60).mean().iloc[-21]) if len(ic) >= 81 else None
        r["idx_r3m"] = ret(ic, 63)
        r["idx_r1y"] = ret(ic, 250)
        # 先對齊到兩邊都有資料的交易日再算報酬；指數缺日時，避免拿多日報酬對個股單日報酬
        last_dates = df.date.tail(250)
        miss = int((~last_dates.isin(ic.index)).sum())
        r["idx_missing"] = miss
        if miss > 5:
            dq.append(f"滬深300 在本檔近 {len(last_dates)} 個交易日中缺 {miss} 天資料，大盤相關指標（Beta、年線、季線）可能失真")
        common = df.date[df.date.isin(ic.index)]
        j = pd.DataFrame({"s": np.log(c.set_axis(df.date)).reindex(common).values,
                          "i": np.log(ic).reindex(common).values}).diff()
        j = j[~bad.set_axis(df.date).reindex(common).values & ~ibad.reindex(common).values].dropna().tail(250)
        r["beta"] = float(j.cov().iloc[0, 1] / j.i.var()) if len(j) > 60 else None
        r["corr"] = float(j.corr().iloc[0, 1]) if len(j) > 60 else None
    else:
        r["idx_close"] = None
    r["dq"] = dq

    # ---------------- 估值與由估值反推的基本面
    pe, pb, ps = df.pe_ttm, df.pb_mrq, df.ps_ttm
    r["has_val"] = bool(pe.notna().any() and (pe != 0).any())
    if r["has_val"]:
        win = df.date >= want_start
        r["pe"], r["pb"], r["ps"] = (float(x.iloc[-1]) if pd.notna(x.iloc[-1]) else None for x in (pe, pb, ps))
        r["pcf"] = float(df.pcf_ttm.iloc[-1]) if pd.notna(df.pcf_ttm.iloc[-1]) else None
        pe_h = pe[win & (pe > 0)]
        pb_h = pb[win & (pb > 0)]
        r["pe_pct"] = float((pe_h < r["pe"]).mean()) if r["pe"] and r["pe"] > 0 and len(pe_h) > 60 else None
        r["pb_pct"] = float((pb_h < r["pb"]).mean()) if r["pb"] and r["pb"] > 0 and len(pb_h) > 60 else None
        r["pe_q"] = [float(pe_h.quantile(q)) for q in (0.2, 0.5, 0.8)] if len(pe_h) > 60 else None
        r["pb_q"] = [float(pb_h.quantile(q)) for q in (0.2, 0.5, 0.8)] if len(pb_h) > 60 else None
        eps = (c / pe).where(pe != 0)          # 前復權尺度的 TTM EPS，不受除權影響
        bps = (c / pb).where(pb > 0)
        sps = (c / ps).where(ps > 0)
        roe = (pb / pe).where((pe > 0) & (pb > 0))
        margin = (ps / pe).where((pe != 0) & (ps > 0))
        hist = []
        for yrs in range(min(3, int(years)), -1, -1):
            row = {"label": "目前" if yrs == 0 else f"{yrs} 年前"}
            for key, s in (("eps", eps), ("bps", bps), ("sps", sps), ("roe", roe), ("margin", margin), ("pe", pe), ("pb", pb)):
                row[key] = at_offset(s, df.date, asof, 365 * yrs) if yrs else (
                    None if pd.isna(s.iloc[-1]) else float(s.iloc[-1]))
            hist.append(row)
        r["fund_hist"] = hist
        now, y1 = hist[-1], next((x for x in hist if x["label"] == "1 年前"), None)
        r["eps"], r["roe"], r["margin"] = now["eps"], now["roe"], now["margin"]
        g = lambda a, b: (a / b - 1) if a is not None and b and b > 0 else None
        r["eps_g"] = g(now["eps"], y1 and y1["eps"])
        r["sps_g"] = g(now["sps"], y1 and y1["sps"])
        r["sps_stale"] = bool(r["sps_g"] is not None and abs(r["sps_g"]) < 0.001)
        r["bps_g"] = g(now["bps"], y1 and y1["bps"])
        y3 = next((x for x in hist if x["label"] == "3 年前"), None)
        r["eps_cagr3"] = ((now["eps"] / y3["eps"]) ** (1 / 3) - 1) if (
            y3 and y3["eps"] and y3["eps"] > 0 and now["eps"] and now["eps"] > 0) else None
        r["peg"] = r["pe"] / (r["eps_g"] * 100) if r["pe"] and r["pe"] > 0 and r["eps_g"] and r["eps_g"] > 0 else None
        if r["pe_q"] and r["eps"] and r["eps"] > 0:
            r["fair"] = [r["eps"] * q for q in r["pe_q"]]
        elif r["pb_q"] and now["bps"]:
            r["fair"] = [now["bps"] * q for q in r["pb_q"]]
            r["fair_by_pb"] = True
        else:
            r["fair"] = None

    # ---------------- 全市場快照：大盤寬度、產業比較
    r["industry"] = None
    ind = data.meta.execute("SELECT industry FROM industry WHERE code=?", (code,)).fetchone()
    if ind and ind[0]:
        r["industry"] = ind[0]
    members = [x[0] for x in data.meta.execute(
        "SELECT index_name FROM index_members WHERE code=? ORDER BY index_name", (code,)).fetchall()]
    r["index_members"] = [INDEX_NAMES.get(x, x) for x in members]
    if not fast and kind == "stock":
        snap_start = (date.fromisoformat(asof) - timedelta(days=130)).isoformat()
        print("讀取全市場資料（大盤寬度、產業比較）…", file=sys.stderr, flush=True)
        snap = data.daily(None, snap_start, asof,
                          "code, date, close, adj_factor, pe_ttm, pb_mrq, amount, tradestatus")
        stocks = {x[0] for x in data.meta.execute("SELECT code FROM securities WHERE kind='stock'")}
        snap = snap[snap.code.isin(stocks) & (snap.tradestatus == 1)].copy()
        snap["hfq"] = snap.close * snap.adj_factor.fillna(1.0)
        g = snap.groupby("code")
        last = g.tail(1).set_index("code")
        last = last[last.date == asof]
        base = g.hfq.apply(lambda s: s.iloc[-64] if len(s) >= 64 else np.nan)   # 63 個交易日前，和本檔一致
        r3 = (last.hfq / base).dropna() - 1
        ma60s = g.hfq.apply(lambda s: s.tail(60).mean() if len(s) >= 60 else np.nan)
        m60 = ma60s.reindex(last.index)
        above = last.hfq[m60.notna()] > m60[m60.notna()]
        r["breadth"] = float(above.mean()) if len(above) >= 30 else None
        r["mkt_r3m_med"] = float(r3.median()) if len(r3) else None
        if r["industry"]:
            peers = [x[0] for x in data.meta.execute(
                "SELECT code FROM industry WHERE industry=?", (r["industry"],)).fetchall()]
            pl_ = last[last.index.isin(peers)]
            pe_p = pl_.pe_ttm[pl_.pe_ttm > 0]
            r["peer_n"] = len(pl_)
            r["peer_pe_med"] = float(pe_p.median()) if len(pe_p) else None
            r["peer_pb_med"] = float(pl_.pb_mrq[pl_.pb_mrq > 0].median()) if (pl_.pb_mrq > 0).any() else None
            r["peer_pe_rank"] = (int((pe_p < r["pe"]).sum()) + 1, len(pe_p)) if r.get("pe") and r["pe"] > 0 and len(pe_p) else None
            r["peer_r3m_med"] = float(r3.reindex(pl_.index).median()) if len(pl_) else None
            big = pl_.sort_values("amount", ascending=False).head(6)
            r["peer_table"] = [(cd, float(row.pe_ttm) if pd.notna(row.pe_ttm) else None,
                                float(row.pb_mrq) if pd.notna(row.pb_mrq) else None,
                                float(r3[cd]) if cd in r3 else None) for cd, row in big.iterrows()]

    # ---------------- 風險旗標
    flags = []
    st = allrows.is_st.iloc[-1] if "is_st" in allrows else 0
    if st == 1:
        flags.append("ST 股（有退市風險，漲跌幅限制 5%）")
    susp = int((allrows.tail(250).tradestatus == 0).sum())
    if susp >= 5:
        flags.append(f"近一年停牌 {susp} 天")
    if r["ipo"] and r["ipo"] > (date.fromisoformat(asof) - timedelta(days=365)).isoformat():
        flags.append(f"上市未滿一年（{r['ipo']}），歷史資料不足")
    if r.get("pe") is not None and r["pe"] < 0:
        flags.append("近四季虧損（PE 為負）")
    if r.get("eps_g") is not None and r["eps_g"] < -0.2:
        flags.append(f"EPS（TTM）年減 {-r['eps_g']:.0%}")
    if r.get("pe_pct") is not None and r["pe_pct"] > 0.9:
        flags.append(f"本益比在近 {years} 年 {r['pe_pct']:.0%} 百分位，估值偏高")
    if r["vol60"] > 0.5:
        flags.append(f"波動大（年化 {r['vol60']:.0%}）")
    if r["amount20"] < 5e7:
        flags.append(f"流動性低（20 日均成交額 {r['amount20'] / 1e8:.2f} 億元）")
    if r["top_div_rsi"] or r["top_div_macd"]:
        flags.append("價格與 " + "／".join(x for x, y in (("RSI", r["top_div_rsi"]), ("MACD", r["top_div_macd"])) if y) + " 頂背離")
    if r["bias20"] > 0.15:
        flags.append(f"股價高於月線 {r['bias20']:.0%}，乖離過大")
    if r["rsi"] > 80:
        flags.append(f"RSI {r['rsi']:.0f}，短線過熱")
    if r["limit_up20"] >= 3:
        flags.append(f"近 20 日漲停 {r['limit_up20']} 次，題材炒作成分高")
    if r["mdd1y"] < -0.4:
        flags.append(f"近一年最大回撤 {r['mdd1y']:.0%}")
    r["flags"] = flags

    # ---------------- 交易計畫
    e = entry_arg or close
    atr_stop = e - 2 * r["atr"]
    s1 = r["support"][0][0] if r["support"] else None
    struct_stop = s1 - 0.25 * r["atr"] if s1 else None
    if struct_stop and r["atr"] <= e - struct_stop <= 3 * r["atr"]:
        stop, why = struct_stop, f"支撐 1（{r['support'][0][1]}）下方 0.25 ATR"
    else:
        stop, why = atr_stop, "進場價 − 2 × ATR"
    # 離進場價不到 1 ATR 的壓力當作「要突破的價位」，目標改用更上面的壓力
    near = [x for x in r["resist"] if x[0] - e < r["atr"]]
    far = [x for x in r["resist"] if x[0] - e >= r["atr"]]
    breakout = near[0] if near else None
    if far:
        t1, t1_why = far[0][0], far[0][1]
    else:
        t1, t1_why = e + 3 * r["atr"], "進場價 + 3 × ATR（上方沒有更遠的壓力）"
    t2, t2_why = (far[1][0], far[1][1]) if len(far) > 1 else (None, None)
    fair_mid = r.get("fair") and r["fair"][1]
    if fair_mid and fair_mid > t1 and (not t2 or fair_mid < t2):
        t2, t2_why = fair_mid, "中性合理價"
    risk_ps = e - stop
    rr = (t1 - e) / risk_ps if risk_ps > 0 else None
    plan = dict(entry=e, stop=stop, stop_why=why, t1=t1, t1_why=t1_why, t2=t2, t2_why=t2_why, rr=rr,
                atr_stop=atr_stop, breakout=breakout)
    if capital and risk_ps > 0:
        lot = 100
        by_risk = math.floor(capital * risk / risk_ps / lot) * lot
        by_cap = math.floor(capital * 0.2 / e / lot) * lot
        plan.update(shares=min(by_risk, by_cap), by_risk=by_risk, by_cap=by_cap, capital=capital, risk=risk)
        plan["cost"] = plan["shares"] * e
        plan["loss"] = plan["shares"] * risk_ps
    r["plan"] = plan

    # ---------------- 自動評分（只評資料庫能判斷的部分）
    sc, why = {}, {}
    if r["idx_close"]:
        pts = [r["idx_ma250"] and r["idx_close"] > r["idx_ma250"], r["idx_ma60_up"],
               r.get("breadth") is not None and r["breadth"] > 0.5,
               r.get("peer_r3m_med") is not None and r.get("idx_r3m") is not None and r["peer_r3m_med"] > r["idx_r3m"]]
        sc["env"] = score(sum(bool(x) for x in pts), 4)
        why["env"] = "、".join(t for t, x in zip(("滬深300在年線上", "滬深300季線上揚", "過半個股在季線上", "產業強於大盤"), pts) if x) or "四項皆未達成"
    if r["has_val"] and r.get("pe") is not None:
        if r["pe"] < 0:
            sc["fund"], why["fund"] = 1, "近四季虧損"
        else:
            p = (2 if (r["eps_g"] or -1) > 0.15 else 1 if (r["eps_g"] or -1) > 0 else 0) + \
                (2 if (r["roe"] or 0) > 0.15 else 1 if (r["roe"] or 0) > 0.08 else 0) + \
                (1 if (r["sps_g"] or -1) > 0 else 0)
            sc["fund"] = score(p, 5)
            why["fund"] = f"EPS 年增 {fmt_pct(r['eps_g'])}、ROE 約 {fmt_pct(r['roe'])}、營收/股 年增 {fmt_pct(r['sps_g'])}（估算）"
        if r["pe"] < 0:
            sc["val"], why["val"] = 1, "虧損無法用本益比評價"
        else:
            p = (2 if r["pe_pct"] is not None and r["pe_pct"] <= 0.3 else 1 if r["pe_pct"] is not None and r["pe_pct"] <= 0.7 else 0) + \
                (2 if r["pb_pct"] is not None and r["pb_pct"] <= 0.3 else 1 if r["pb_pct"] is not None and r["pb_pct"] <= 0.7 else 0) + \
                (1 if r.get("peer_pe_med") and r["pe"] < r["peer_pe_med"] else 0) + (1 if r["peg"] and r["peg"] < 1 else 0)
            sc["val"] = score(p, 6)
            why["val"] = f"PE 百分位 {fmt_pct(r['pe_pct'])}、PB 百分位 {fmt_pct(r['pb_pct'])}、PEG {fmt(r['peg'])}"
    tech = [("股價在季線上且季線上揚", close > (r["ma60"] or 1e18) and bool(r["ma60_up"])),
            ("均線多頭排列", r["bull_align"]),
            ("週線在 20 週均線上", bool(r["w_above_ma20"])),
            ("RSI 在 45～75", 45 <= r["rsi"] <= 75),
            ("MACD 快線在慢線上", r["dif"] > r["dea"]),
            ("沒有頂背離", not (r["top_div_rsi"] or r["top_div_macd"])),
            ("上漲量大於下跌量", (r["updown_vol"] or 0) > 1),
            ("離月線不到 10%", r["bias20"] < 0.10)]
    r["tech_checks"] = tech
    sc["tech"] = score(sum(x for _, x in tech), len(tech))
    why["tech"] = f"{sum(x for _, x in tech)}/{len(tech)} 項通過"
    if rr is not None:
        sc["plan"] = 5 if rr >= 3 else 4 if rr >= 2 else 3 if rr >= 1.5 else 2 if rr >= 1 else 1
        why["plan"] = f"風險報酬比 {rr:.1f}"
    r["scores"], r["score_why"] = sc, why
    return r


# ====================================================================== 報告
def fmt(x, nd=2):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.{nd}f}"


def fmt_pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.0%}"


def sign_pct(x):
    return "—" if x is None else f"{x:+.1%}"


def yn(x):
    return "—" if x is None else ("是" if x else "否")


def decide(sc):
    total = sum(sc[k] * w for k, _, w in SECTIONS if k in sc)
    wsum = sum(w for k, _, w in SECTIONS if k in sc)
    veto = [n for k, n, _ in SECTIONS if k in ("fund", "plan") and k in sc and sc[k] <= 2]
    complete = all(k in sc for k, _, _ in SECTIONS)
    if veto:
        dec = "不買（" + "、".join(veto) + " 只有 1～2 分，一票否決）"
    elif not complete:
        dec = "尚未完成：請補上人工評分（--chips、--catalyst）"
    else:
        dec = "照交易計畫買進" if total >= 3.8 else "小部位試單或觀察" if total >= 3.0 else "不買，列入觀察名單"
    return total, wsum, dec, complete


def report(r, manual):
    sc = dict(r["scores"])
    sc.update({k: v for k, v in manual.items() if v})
    L = []
    w = L.append
    w(f"# {r['name']}（{r['code']}）分析報告")
    w("")
    w(f"資料日期 {r['asof']}｜回看 {r['years']} 年（資料起點 {r['start']}）｜由 tools/analyze.py 產生，不構成投資建議")
    w("")
    if r["short_history"]:
        w(f"> 資料起點晚於 {r['years']} 年前：可能是上市較晚，或資料夾裡缺少較早的數據包；百分位與趨勢只依現有資料計算。")
        w("")
    total, wsum, dec, complete = decide(sc)
    w("## 結論")
    w("")
    w(f"- **決策**：{dec}")
    if complete:
        w(f"- **加權總分**：{total:.2f}")
    elif wsum:
        w(f"- **已評部分平均**：{total / wsum:.2f}（已評 {wsum:.0%} 權重，其餘待人工評分）")
    if r["flags"]:
        w(f"- **風險旗標**：{len(r['flags'])} 個，見第六步")
    if r["dq"]:
        w(f"- **資料品質**：{len(r['dq'])} 則提醒，見第六步")
    w("")

    w("## 基本資料")
    w("")
    w("| 項目 | 數值 |")
    w("| --- | --- |")
    w(f"| 收盤價（不復權） | {fmt(r['close_raw'])}（當日 {sign_pct(r['pct_chg'] / 100 if r['pct_chg'] is not None else None)}） |")
    w(f"| 產業（證監會分類） | {r['industry'] or '—'} |")
    w(f"| 指數成分 | {'、'.join(r['index_members']) or '—'} |")
    w(f"| 上市日 | {r['ipo'] or '—'} |")
    w(f"| 52 週高 / 低 | {fmt(r['hi52'])} / {fmt(r['lo52'])}（距高點 {sign_pct(r['from_hi52'])}） |")
    w(f"| 20 日均成交額 | {r['amount20'] / 1e8:,.2f} 億元 |")
    w(f"| 報酬 1 週 / 1 月 / 3 月 / 6 月 / 1 年 | {' / '.join(sign_pct(r[k]) for k in ('r1w', 'r1m', 'r3m', 'r6m', 'r1y'))} |")
    w("")
    w("以下價格都是前復權價（以資料日期當天為基準，與目前股價同一尺度）。")
    w("")

    w("## 第一步：總體與產業環境（自動部分）")
    w("")
    if r["idx_close"]:
        w("| 項目 | 數值 | 判斷 |")
        w("| --- | --- | --- |")
        w(f"| 滬深300 vs 年線 | {fmt(r['idx_close'])} / {fmt(r['idx_ma250'])} | {'年線上' if r['idx_ma250'] and r['idx_close'] > r['idx_ma250'] else '年線下'} |")
        w(f"| 滬深300 季線方向 | {fmt(r['idx_ma60'])} | {'上揚' if r['idx_ma60_up'] else '下彎'} |")
        w(f"| 滬深300 近 3 月 / 1 年 | {sign_pct(r['idx_r3m'])} / {sign_pct(r['idx_r1y'])} | |")
        if r.get("breadth") is not None:
            w(f"| 站上季線的個股比例 | {r['breadth']:.0%} | {'偏多' if r['breadth'] > 0.5 else '偏空'} |")
        if r.get("peer_r3m_med") is not None:
            w(f"| 產業近 3 月中位數報酬 | {sign_pct(r['peer_r3m_med'])} | {'強於' if r['peer_r3m_med'] > (r['idx_r3m'] or 0) else '弱於'}滬深300 |")
        w("")
    w("需人工判斷：利率、匯率、景氣、政策，以及產業週期位置與供需。")
    w("")

    w("## 第二步：基本面（由估值反推的估算）")
    w("")
    if r["has_val"] and r.get("fund_hist"):
        w("| 指標 | " + " | ".join(x["label"] for x in r["fund_hist"]) + " |")
        w("| --- | " + " | ".join("---" for _ in r["fund_hist"]) + " |")
        for key, label, pct in (("eps", "EPS（TTM，前復權）", False), ("bps", "每股淨值", False),
                                ("sps", "每股營收（TTM）", False), ("roe", "ROE ≈ PB ÷ PE", True),
                                ("margin", "淨利率 ≈ PS ÷ PE", True)):
            w(f"| {label} | " + " | ".join(
                (f"{x[key]:.1%}" if pct else fmt(x[key])) if x[key] is not None else "—" for x in r["fund_hist"]) + " |")
        w("")
        w(f"EPS 年增 {sign_pct(r['eps_g'])}，3 年年化 {sign_pct(r['eps_cagr3'])}；每股營收年增 {sign_pct(r['sps_g'])}；每股淨值年增 {sign_pct(r['bps_g'])}。")
        w("")
        if r.get("sps_stale"):
            w("每股營收和一年前幾乎一樣：資料源的 PS 可能還沒更新最新營收，營收成長請以財報為準。")
            w("")
        w("這些是由 PE、PB、PS 反推的近似值，會受股本變動與財報公布時點影響；毛利率、現金流、負債比、護城河仍要看財報。")
    else:
        w("沒有估值資料（ETF、指數或資料缺漏），需人工看財報。")
    w("")

    w("## 第三步：估值")
    w("")
    if r["has_val"] and r.get("pe") is not None:
        w(f"| 指標 | 目前 | 近 {r['years']} 年 20% / 50% / 80% 分位 | 目前百分位 | 同業中位數 |")
        w("| --- | --- | --- | --- | --- |")
        q = lambda xs: " / ".join(fmt(x, 1) for x in xs) if xs else "—"
        w(f"| 本益比 PE（TTM） | {fmt(r['pe'], 1)} | {q(r['pe_q'])} | {fmt_pct(r['pe_pct'])} | {fmt(r.get('peer_pe_med'), 1)} |")
        w(f"| 股價淨值比 PB | {fmt(r['pb'], 2)} | {q(r['pb_q'])} | {fmt_pct(r['pb_pct'])} | {fmt(r.get('peer_pb_med'), 2)} |")
        w(f"| 股價營收比 PS（TTM） | {fmt(r['ps'], 2)} | | | |")
        w(f"| 市現率 PCF（TTM，現金淨流量） | {fmt(r['pcf'], 1)} | | | |")
        w(f"| PEG | {fmt(r['peg'])} | | | |")
        w("")
        if r.get("peer_pe_rank"):
            w(f"本益比在同產業 {r['peer_pe_rank'][1]} 檔獲利公司中排第 {r['peer_pe_rank'][0]} 低。")
            w("")
        if r.get("fair"):
            basis = "每股淨值 × PB 分位" if r.get("fair_by_pb") else "EPS（TTM）× PE 分位"
            w(f"### 合理價試算（{basis}）")
            w("")
            w("| 情境 | 合理價 | 與現價差距 |")
            w("| --- | --- | --- |")
            for lab, p in zip(("悲觀（20% 分位）", "中性（50% 分位）", "樂觀（80% 分位）"), r["fair"]):
                w(f"| {lab} | {fmt(p)} | {sign_pct(p / r['close'] - 1)} |")
            w("")
            w("用的是近四季 EPS，沒有計入未來成長；成長股應再用預估 EPS 試算。")
            w("")
        if r.get("peer_table"):
            w("### 同產業成交額前幾大")
            w("")
            w("| 代碼 | PE | PB | 近 3 月報酬 |")
            w("| --- | --- | --- | --- |")
            for cd, a, b, c3 in r["peer_table"]:
                mark = "（本檔）" if cd == r["code"] else ""
                w(f"| {cd}{mark} | {fmt(a, 1)} | {fmt(b, 2)} | {sign_pct(c3)} |")
            w("")
    else:
        w("沒有估值資料。")
        w("")

    w("## 第四步：籌碼面（代理指標，需人工評分）")
    w("")
    w("資料庫沒有融資融券、北向資金、股東人數與龍虎榜，以下只是量價代理指標。")
    w("")
    w("| 項目 | 數值 | 解讀 |")
    w("| --- | --- | --- |")
    w(f"| 5 日 / 20 日均量 | {fmt(r['vol_ratio'])} 倍 | {'放量' if (r['vol_ratio'] or 0) > 1.5 else '縮量' if (r['vol_ratio'] or 1) < 0.7 else '正常'} |")
    w(f"| 近 20 日上漲量 / 下跌量 | {fmt(r['updown_vol'])} | {'買盤較強' if (r['updown_vol'] or 0) > 1 else '賣壓較強'} |")
    w(f"| OBV 在 20 日均線上 | {yn(r['obv_up'])} | {'資金流入' if r['obv_up'] else '資金流出'} |")
    if r["turn5"] is not None:
        w(f"| 換手率 5 日 / 60 日平均 | {fmt(r['turn5'])}% / {fmt(r['turn60'])}% | {'交投轉熱' if r['turn5'] > 1.5 * r['turn60'] else '平穩'} |")
    w(f"| 近 20 日漲停 / 跌停 | {r['limit_up20']} / {r['limit_dn20']} 次 | |")
    w("")

    w("## 第五步：技術面")
    w("")
    w("| 項目 | 數值 | 判斷 |")
    w("| --- | --- | --- |")
    w(f"| 均線 5 / 20 / 60 / 250 | {' / '.join(fmt(r[f'ma{n}']) for n in (5, 20, 60, 250))} | "
      f"{'多頭排列' if r['bull_align'] else '空頭排列' if r['bear_align'] else '糾結'} |")
    w(f"| 月線 / 季線方向 | | 月線{'上揚' if r['ma20_up'] else '下彎'}、季線{'上揚' if r['ma60_up'] else '下彎'} |")
    w(f"| 週線 | | {'在' if r['w_above_ma20'] else '不在'} 20 週均線上、週 MACD {'多' if r['w_macd_bull'] else '空'}方 |")
    w(f"| 高低點 | | {'高點、低點都墊高' if r['hh_hl'] else '高點、低點都降低' if r['lh_ll'] else '沒有明確方向'} |")
    w(f"| RSI（14） | {fmt(r['rsi'], 1)} | {'過熱' if r['rsi'] > 70 else '超賣' if r['rsi'] < 30 else '偏強' if r['rsi'] >= 50 else '偏弱'} |")
    w(f"| MACD 快線 / 慢線 / 柱 | {fmt(r['dif'], 3)} / {fmt(r['dea'], 3)} / {fmt(r['bar'], 3)} | "
      f"{'多方' if r['dif'] > r['dea'] else '空方'}，柱狀體{'放大' if abs(r['bar']) > abs(r['bar_prev']) else '縮小'} |")
    kx = "黃金交叉" if r["k_prev"] <= r["d_prev"] and r["k"] > r["d"] else "死亡交叉" if r["k_prev"] >= r["d_prev"] and r["k"] < r["d"] else ""
    w(f"| KD（9,3,3） | {fmt(r['k'], 1)} / {fmt(r['d'], 1)} | {kx or ('K 在 D 上' if r['k'] > r['d'] else 'K 在 D 下')}{'，高檔' if r['k'] > 80 else '，低檔' if r['k'] < 20 else ''} |")
    w(f"| 布林通道 上 / 中 / 下 | {fmt(r['boll_up'])} / {fmt(r['boll_mid'])} / {fmt(r['boll_dn'])} | "
      f"%b {fmt(r['boll_pctb'])}，帶寬在一年 {r['boll_width_pct']:.0%} 分位{'（收斂，留意變盤）' if r['boll_width_pct'] < 0.2 else ''} |")
    w(f"| ATR（14） | {fmt(r['atr'])}（{r['atr_pct']:.1%}） | 每日正常波動 |")
    w(f"| 月線乖離 | {sign_pct(r['bias20'])} | |")
    div = [t for t, x in (("RSI 頂背離", r["top_div_rsi"]), ("MACD 頂背離", r["top_div_macd"]),
                          ("RSI 底背離", r["bot_div_rsi"]), ("MACD 底背離", r["bot_div_macd"])) if x]
    w(f"| 背離 | | {'、'.join(div) or '無'} |")
    w(f"| 年化波動 / Beta / 一年最大回撤 | {r['vol60']:.0%} / {fmt(r.get('beta'))} / {r['mdd1y']:.0%} | 與滬深300 相關係數 {fmt(r.get('corr'))} |")
    w("")
    w("### 關鍵價位")
    w("")
    w("| 價位 | 價格 | 依據 |")
    w("| --- | --- | --- |")
    for i, (p, why) in reversed(list(enumerate(r["resist"], 1))):
        w(f"| 壓力 {i} | {fmt(p)} | {why} |")
    w(f"| 目前股價 | {fmt(r['close'])} | |")
    for i, (p, why) in enumerate(r["support"], 1):
        w(f"| 支撐 {i} | {fmt(p)} | {why} |")
    w("")
    w("### 技術面檢查")
    w("")
    for t, x in r["tech_checks"]:
        w(f"- [{'x' if x else ' '}] {t}")
    w("")

    w("## 第六步：風險旗標")
    w("")
    if r["flags"]:
        for f in r["flags"]:
            w(f"- {f}")
    else:
        w("資料庫能偵測的風險旗標都沒有出現。")
    w("")
    w("催化劑（財報、新產品、政策、指數調整）與反方論點需人工整理。")
    w("")
    if r["dq"]:
        w("### 資料品質提醒")
        w("")
        w("以下資料可能有問題（單日漲跌超過漲跌停的資料點已排除在波動率與 Beta 之外）：")
        w("")
        for x in r["dq"]:
            w(f"- {x}")
        w("")

    p = r["plan"]
    w("## 第七步：交易計畫")
    w("")
    w("| 項目 | 價格 | 說明 |")
    w("| --- | --- | --- |")
    w(f"| 進場價 | {fmt(p['entry'])} | {'指定價' if p['entry'] != r['close'] else '最新收盤'} |")
    w(f"| 停損價 | {fmt(p['stop'])} | {p['stop_why']}（{sign_pct(p['stop'] / p['entry'] - 1)}） |")
    w(f"| 第一目標 | {fmt(p['t1'])} | {p['t1_why']}（{sign_pct(p['t1'] / p['entry'] - 1)}） |")
    if p["t2"]:
        w(f"| 第二目標 | {fmt(p['t2'])} | {p['t2_why']}（{sign_pct(p['t2'] / p['entry'] - 1)}） |")
    w(f"| 風險報酬比 | {fmt(p['rr'], 1)} | 以第一目標計算，至少要 2 |")
    w("")
    if p["breakout"]:
        w(f"上方 {fmt(p['breakout'][0])}（{p['breakout'][1]}）離現價不到 1 個 ATR：這是突破型進場，"
          "最好等收盤站上這個價位再買，跌回下方就代表突破失敗。")
        w("")
    if "shares" in p:
        w(f"總資金 {p['capital']:,.0f} 元、單筆風險 {p['risk']:.1%}：依風險可買 {p['by_risk']:,} 股，"
          f"單一個股上限 20% 為 {p['by_cap']:,} 股，建議 **{p['shares']:,} 股**（約 {p['cost']:,.0f} 元；觸及停損約虧 {p['loss']:,.0f} 元）。")
    else:
        w("加上 --capital 總資金 可以算出建議股數。")
    w("")

    w("## 總評分表")
    w("")
    w("| 面向 | 權重 | 評分 | 依據 |")
    w("| --- | --- | --- | --- |")
    for k, n, wt in SECTIONS:
        src = "人工" if k in manual and manual[k] else "自動"
        note = r["score_why"].get(k, "") if src == "自動" else "人工評分"
        w(f"| {n} | {wt:.0%} | {sc.get(k, '待填')} | {note if k in sc else '需人工評分'} |")
    w(f"| **合計** | **100%** | **{total:.2f}** | {dec} |" if complete else
      f"| **已評部分平均** | **{wsum:.0%}** | **{total / wsum:.2f}** | {dec} |" if wsum else
      f"| **合計** | **100%** | **—** | {dec} |")
    w("")
    w("規則：加權總分 3.8 以上照計畫買進，3.0～3.7 小部位試單或觀察，低於 3.0 不買；基本面或交易計畫只有 1～2 分時一票否決。"
      "自動評分只看資料庫有的數據，請依你的判斷修正。")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="從 market-data 資料庫產生個股分析報告")
    ap.add_argument("code", help="股票代碼或名稱，例如 600519、sz.000858、贵州茅台")
    ap.add_argument("--dir", default=".", help="放 meta.db 與 astock_*.db(.gz) 的資料夾（預設目前資料夾）")
    ap.add_argument("--date", help="分析日期 YYYY-MM-DD（預設最新一天）")
    ap.add_argument("--years", type=float, default=5, help="估值百分位回看年數（預設 5）")
    ap.add_argument("--capital", type=float, help="總資金（元），用來計算建議股數")
    ap.add_argument("--risk", type=float, default=0.01, help="單筆風險比例（預設 0.01 = 1%%）")
    ap.add_argument("--entry", type=float, help="計畫進場價（預設最新收盤價）")
    ap.add_argument("--chips", type=int, choices=range(1, 6), help="第四步 籌碼面 人工評分 1～5")
    ap.add_argument("--catalyst", type=int, choices=range(1, 6), help="第六步 催化劑與風險 人工評分 1～5")
    ap.add_argument("--env", type=int, choices=range(1, 6), help="覆蓋第一步自動評分")
    ap.add_argument("--fund", type=int, choices=range(1, 6), help="覆蓋第二步自動評分（看完財報後）")
    ap.add_argument("--fast", action="store_true", help="略過全市場掃描（不算大盤寬度與產業比較）")
    ap.add_argument("--out", help="報告路徑（預設 reports/<代碼>_<日期>.md）")
    a = ap.parse_args(argv)
    data = Data(a.dir)
    code, name, kind = resolve(data, a.code)
    r = analyze(data, code, name, kind, a.date, a.years, a.capital, a.risk, a.entry, a.fast)
    manual = {"chips": a.chips, "cat": a.catalyst, "env": a.env, "fund": a.fund}
    md = report(r, manual)
    out = Path(a.out) if a.out else Path("reports") / f"{code.replace('.', '')}_{r['asof']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    try:
        print(md)
    except UnicodeEncodeError:      # Windows 舊主控台
        sys.stdout.buffer.write(md.encode("utf-8"))
    print(f"報告已存到 {out}", file=sys.stderr)
    return r


if __name__ == "__main__":
    main()
