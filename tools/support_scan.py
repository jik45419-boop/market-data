#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
支撐位反覆反彈掃描：用 Drive 上的全市場日線（2005 起、精確復權、含退市股）找出
「跌到同一個價格帶就反彈、來回反覆」的股票，結果存回 Drive 的 market-data 資料夾。

  support_scan_YYYY-MM-DD.csv        目前符合型態的股票（近 WINDOW 個交易日）
  support_scan_stats_YYYY-MM-DD.csv  歷史統計：支撐帶成形後，下一次回測守住的比例（逐年）

判斷方法
  轉折點：後復權收盤價從高點回落 DOWN 以上算一個低點，從低點反彈 UP 以上算一個高點
  支撐帶：多個低點落在同一個 BAND 寬的價格帶內，且之後的最低收盤價不低於帶底約 9%

環境變數：FORCE=1 表示同一天的結果已存在也重算；LOCAL_STORE 指向本地資料夾時不連 Drive（測試用）
"""
import csv
import os
import pickle
import sqlite3
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DATA, STOCK, get_state, gunzip_file, notice, pack_get  # noqa: E402
from packing import existing  # noqa: E402
from store import get_store  # noqa: E402

UP, DOWN = 0.18, 0.15       # 轉折門檻
BAND = 1.15                 # 支撐帶寬度（帶頂 / 帶底）
WINDOW = 700                # 目前名單看最近幾個交易日
MIN_ROWS = 400              # 至少要有這麼多交易日才列入
MIN_TOUCH = 4               # 至少觸底反彈幾次
MIN_SPAN = 250              # 首尾觸底至少相隔幾個交易日
CACHE = DATA / "scan_cache"
PREFIX = "support_scan_"


# ---------------------------------------------------------------- 型態判斷
def zigzag(c, up=UP, down=DOWN):
    """回傳 (轉折點 [(類型 'H'/'L', 索引)], 目前方向 1 漲 / -1 跌 / 0 未定, 目前這段的極值索引)"""
    piv, d, hi, lo, ei = [], 0, 0, 0, 0
    for i, x in enumerate(c):
        if d == 0:
            if x > c[hi]:
                hi = i
            if x < c[lo]:
                lo = i
            if x >= c[lo] * (1 + up):
                piv.append(("L", lo)); d, ei = 1, i
            elif x <= c[hi] * (1 - down):
                piv.append(("H", hi)); d, ei = -1, i
        elif d == 1:
            if x > c[ei]:
                ei = i
            elif x <= c[ei] * (1 - down):
                piv.append(("H", ei)); d, ei = -1, i
        else:
            if x < c[ei]:
                ei = i
            elif x >= c[ei] * (1 + up):
                piv.append(("L", ei)); d, ei = 1, i
    return piv, d, ei


def support_band(c):
    """在價格序列 c 找觸底次數最多、且之後沒被有效跌破的支撐帶；找不到回傳 None"""
    piv, d, ei = zigzag(c)
    lows = [i for t, i in piv if t == "L"]
    highs = [i for t, i in piv if t == "H"]
    best = None
    for a in lows:
        grp = [i for i in lows if c[a] <= c[i] <= c[a] * BAND]
        if c[a] > min(c[grp[0]:]) * 1.10:        # 支撐帶必須是區間底部
            continue
        if best is None or len(grp) > len(best) or (len(grp) == len(best) and grp[-1] > best[-1]):
            best = grp
    if not best:
        return None
    bounces = []
    for i in best:
        nxt = [h for h in highs if h > i]
        if nxt:
            bounces.append(c[nxt[0]] / c[i] - 1)
        elif d == 1:
            bounces.append(c[ei] / c[i] - 1)
    if not bounces:
        return None
    return dict(touches=best, lo=min(c[i] for i in best), hi=max(c[i] for i in best),
                bounces=bounces, direction=d)


def retests(c):
    """歷史回測：連續 3 個低點成帶（WINDOW 內）之後，下一個低點若回到帶內算一次回測。
    回傳 [(回測低點索引, 是否守住)]；守住 = 沒有跌破帶底 7% 以上"""
    piv, _, _ = zigzag(c)
    lows = [i for t, i in piv if t == "L"]
    out = []
    for j in range(3, len(lows)):
        if lows[j] - lows[j - 3] > WINDOW:
            continue
        p = [c[i] for i in lows[j - 3:j]]
        lo, hi = min(p), max(p)
        if hi / lo <= BAND and c[lows[j]] <= hi * 1.05:
            out.append((lows[j], c[lows[j]] >= lo * 0.93))
    return out


# ---------------------------------------------------------------- 讀取數據包
class Series:
    __slots__ = ("d", "c", "st", "pe", "pb", "adj")

    def __init__(self):
        self.d, self.c = array("i"), array("d")          # 日期 YYYYMMDD、後復權收盤
        self.st, self.pe, self.pb = array("b"), array("f"), array("f")
        self.adj = 1.0                                   # 最新的後復權因子


def read_pack(path, stocks):
    """數據包 → {code: (日期, 後復權收盤, is_st, pe_ttm, pb_mrq, 最新復權因子)}（只取有交易的股票日線）"""
    out = {}
    conn = sqlite3.connect(str(path))
    cur = conn.execute("SELECT code, date, close, adj_factor, is_st, pe_ttm, pb_mrq FROM daily "
                       "WHERE COALESCE(tradestatus, 1) = 1 AND close > 0 ORDER BY code, date")
    for code, dt, close, adj, st, pe, pb in cur:
        if code not in stocks:
            continue
        s = out.get(code)
        if s is None:
            s = out[code] = Series()
        if adj and adj > 0:
            s.adj = adj
        s.d.append(int(dt.replace("-", "")))
        s.c.append(close * s.adj)
        s.st.append(1 if st else 0)
        s.pe.append(pe or 0.0)
        s.pb.append(pb or 0.0)
    conn.close()
    return {k: (v.d.tobytes(), v.c.tobytes(), v.st.tobytes(), v.pe.tobytes(), v.pb.tobytes(), v.adj)
            for k, v in out.items()}


def load_all(store, stocks):
    """依時間順序讀所有數據包；已經結束的包（不是最新的）抽出的序列會留在 CACHE，下次不用再下載"""
    CACHE.mkdir(parents=True, exist_ok=True)
    packs = existing(store)
    names = {n for _, _, n in packs}
    for f in CACHE.glob("*.pkl"):
        if f.stem not in names:
            f.unlink()
    data = {}
    for k, (_, _, name) in enumerate(packs):
        pkl = CACHE / f"{name}.pkl"
        latest = k == len(packs) - 1
        if pkl.exists() and not latest:
            part = pickle.loads(pkl.read_bytes())
            print(f"{name}: 用快取（{len(part)} 檔）", flush=True)
        else:
            db = DATA / f"{name}.db"
            if not pack_get(store, name, db):
                raise RuntimeError(f"Drive 上找不到 {name}")
            part = read_pack(db, stocks)
            os.remove(db)
            if not latest:
                pkl.write_bytes(pickle.dumps(part, protocol=4))
            print(f"{name}: 已下載（{len(part)} 檔）", flush=True)
        for code, (d, c, st, pe, pb, adj) in part.items():
            s = data.get(code)
            if s is None:
                s = data[code] = Series()
            s.d.frombytes(d); s.c.frombytes(c); s.st.frombytes(st); s.pe.frombytes(pe); s.pb.frombytes(pb)
            s.adj = adj
    return data


# ---------------------------------------------------------------- 分析
def fmt_date(n):
    return f"{n // 10000}-{n // 100 % 100:02d}-{n % 100:02d}"


def analyse(data, info, last_date):
    """回傳 (目前名單 rows, 歷史統計 {年: {組: [回測次數, 守住次數]}})"""
    rows, stats = [], {}
    recent = sorted({s.d[-1] for s in data.values()})[-10:][0] if data else 0   # 最近 10 個有資料的日期內算「還在交易」
    for code, s in data.items():
        c = list(s.c)
        # 歷史回測（全部歷史、含退市股）
        for i, held in retests(c):
            year = s.d[i] // 10000
            groups = ["全部"]
            groups.append("當時非ST且盈利" if (not s.st[i] and s.pe[i] > 0) else "當時ST或虧損")
            for g in groups:
                a = stats.setdefault(year, {}).setdefault(g, [0, 0])
                a[0] += 1
                a[1] += held
        # 目前名單
        name, status, industry = info.get(code, ("", 1, ""))
        if len(c) < MIN_ROWS or s.d[-1] < recent or not status or s.st[-1]:
            continue
        off = max(0, len(c) - WINDOW)
        w = c[off:]
        b = support_band(w)
        if not b or len(b["touches"]) < MIN_TOUCH or b["touches"][-1] - b["touches"][0] < MIN_SPAN:
            continue
        q = 1.0 / s.adj                                   # 後復權 → 前復權（以最新一天為基準）
        last = w[-1] * q
        mid = (b["lo"] + b["hi"]) / 2 * q
        rows.append([
            code, name, industry, len(b["touches"]), round(b["lo"] * q, 2), round(b["hi"] * q, 2),
            fmt_date(s.d[off + b["touches"][0]]), fmt_date(s.d[off + b["touches"][-1]]),
            f"{sum(b['bounces']) / len(b['bounces']):.1%}", f"{min(b['bounces']):.1%}",
            round(last, 2), f"{last / mid - 1:.1%}", "漲" if b["direction"] == 1 else "跌",
            round(s.pe[-1], 1), round(s.pb[-1], 2), fmt_date(s.d[-1]),
        ])
    rows.sort(key=lambda r: (-r[3], -float(r[8].rstrip("%"))))
    return rows, stats


LIST_HEAD = ["代碼", "名稱", "行業", "觸底反彈次數", "支撐帶下緣", "支撐帶上緣", "首次觸底", "最近觸底",
             "平均反彈幅度", "最小反彈幅度", "現價(前復權)", "現價距支撐帶中值", "目前方向",
             "市盈率TTM", "市淨率", "數據日期"]


def write_csv(path, head, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


def stats_rows(stats):
    out, total = [], {}
    for year in sorted(stats):
        for g, (n, h) in sorted(stats[year].items()):
            out.append([year, g, n, h, f"{h / n:.1%}"])
            t = total.setdefault(g, [0, 0])
            t[0] += n
            t[1] += h
    for g, (n, h) in sorted(total.items()):
        out.append(["合計", g, n, h, f"{h / n:.1%}"])
    return out


def summary(rows, srows, date):
    near = [r for r in rows if r[13] > 0 and -8 <= float(r[11].rstrip("%")) <= 10]
    lines = [f"## 支撐位反覆反彈掃描（數據到 {date}）", "",
             f"- 符合型態：**{len(rows)}** 檔（觸底 ≥{MIN_TOUCH} 次、相隔 ≥{MIN_SPAN} 個交易日、近 {WINDOW} 個交易日）",
             f"- 其中盈利且現價貼近支撐帶（中值 -8% ~ +10%）：**{len(near)}** 檔", "",
             "### 歷史統計：支撐帶成形後，下一次回測守住的比例", "",
             "| 年 | 組別 | 回測次數 | 守住 | 比例 |", "|---|---|---|---|---|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |" for r in srows if r[0] == "合計"]
    lines += ["", "### 盈利且現價貼近支撐帶（前 40 檔）", "",
              "| 代碼 | 名稱 | 行業 | 觸底 | 支撐帶 | 平均反彈 | 現價 | 距支撐 | 市盈率 |", "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]}–{r[5]} | {r[8]} | {r[10]} | {r[11]} | {r[13]} |"
              for r in near[:40]]
    return "\n".join(lines) + "\n"


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    store = get_store()
    store.download("meta.db.gz", DATA / "meta.db.gz")
    gunzip_file(DATA / "meta.db.gz", DATA / "meta.db")
    meta = sqlite3.connect(str(DATA / "meta.db"))
    date = get_state(meta, "daily_data_date")
    if not date:
        notice("支撐掃描", "meta 裡沒有數據日期，略過", "warning")
        return
    listed = [r[0] for r in meta.execute("SELECT code FROM securities WHERE kind=? AND status=1", (STOCK,))]
    done = {r[0] for r in meta.execute(
        "SELECT code FROM progress WHERE dataset='daily' AND done_until>=?", (date,))}
    ready = sum(c in done for c in listed) / max(len(listed), 1)
    force = os.environ.get("FORCE", "").strip() in ("1", "true")
    if ready < 0.95 and not force:
        notice("支撐掃描", f"{date} 的日線只更新了 {ready:.0%}，等更新完再掃描")
        return
    if store.exists(f"{PREFIX}{date}.csv") and not force:
        notice("支撐掃描", f"{date} 的結果已經在 Drive 上，略過")
        return

    info = {}
    industry = dict(meta.execute("SELECT code, industry FROM industry"))
    stocks = set()
    for code, name, status in meta.execute("SELECT code, name, status FROM securities WHERE kind=?", (STOCK,)):
        stocks.add(code)
        info[code] = (name, status, industry.get(code) or "")
    data = load_all(store, stocks)
    rows, stats = analyse(data, info, date)
    srows = stats_rows(stats)

    out1, out2 = DATA / f"{PREFIX}{date}.csv", DATA / f"{PREFIX}stats_{date}.csv"
    write_csv(out1, LIST_HEAD, rows)
    write_csv(out2, ["年", "組別", "回測次數", "守住次數", "守住比例"], srows)
    old = [n for n in store.files if n.startswith(PREFIX) and n not in (out1.name, out2.name)]
    store.upload(out1, out1.name, mimetype="text/csv")
    store.upload(out2, out2.name, mimetype="text/csv")
    for n in old:                                # 只留最新一天的結果
        store.delete(n)

    text = summary(rows, srows, date)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    notice("支撐掃描", f"{date}：{len(data)} 檔股票，符合型態 {len(rows)} 檔，已存到 Drive：{out1.name}、{out2.name}")


if __name__ == "__main__":
    main()
