#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions 上跑個股分析：從 Drive 下載需要的數據包，精簡成分析用的小資料庫，再對每一檔跑 analyze.py

環境變數：
  CODES    要分析的股票，逗號分隔（代碼或簡體名稱）
  YEARS    回看年數（預設 5）
  CAPITAL  計算建議股數用的總資金（預設 1000000）
每次只保留一個完整數據包在硬碟上：下載 → 解壓 → 挑出需要的列 → 刪掉大檔。
"""
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
from common import SCHEMA, gunzip_file, pack_get  # noqa: E402
from store import get_store  # noqa: E402

OUT = Path(os.environ.get("ANALYZE_DIR", "analyze_data"))
REPORTS = Path("reports")


class _Meta:
    def __init__(self, path):
        self.meta = sqlite3.connect(path)


def main():
    codes_in = [c.strip() for c in re.split(r"[,，\s]+", os.environ.get("CODES", "")) if c.strip()]
    if not codes_in:
        sys.exit("CODES 沒有指定股票")
    years = float(os.environ.get("YEARS") or 5)
    capital = os.environ.get("CAPITAL") or "1000000"
    OUT.mkdir(parents=True, exist_ok=True)
    st = get_store()

    st.download("meta.db.gz", OUT / "meta.db.gz")
    gunzip_file(OUT / "meta.db.gz", OUT / "meta.db")
    (OUT / "meta.db.gz").unlink()
    m = _Meta(OUT / "meta.db")
    targets = [analyze.resolve(m, c) for c in codes_in]
    keep = sorted({t[0] for t in targets} | {analyze.HS300} |
                  ({"sh.000016", "sh.000001", "sz.399001", "sz.399006"} if os.environ.get("DIAG") else set()))
    print("分析對象：" + "、".join(f"{c} {n}" for c, n, _ in targets), flush=True)

    names = sorted({analyze.PACK_RE.match(n).group(1) for n in st.files if analyze.PACK_RE.match(n)},
                   key=lambda n: analyze._span(n)[1], reverse=True)          # 新的先處理
    latest = snap_start = start_ym = None
    for name in names:
        lo, hi = analyze._span(name)
        if start_ym and hi < start_ym:
            continue
        big = OUT / f"{name}.full.db"
        print(f"下載 {name} …", flush=True)
        if not pack_get(st, name, big):
            continue
        src = sqlite3.connect(big)
        if latest is None:
            latest = src.execute("SELECT MAX(date) FROM daily").fetchone()[0]
            snap_start = (date.fromisoformat(latest) - timedelta(days=140)).isoformat()
            first = (date.fromisoformat(latest) - timedelta(days=int(365.25 * years) + 40)).isoformat()
            start_ym = analyze._ym(first)
            print(f"最新資料日 {latest}；需要 {first[:7]} 之後的數據包", flush=True)
        src.close()
        slim = OUT / f"{name}.db"
        con = sqlite3.connect(slim)
        con.execute(SCHEMA["daily"])
        con.execute("ATTACH DATABASE ? AS src", (str(big),))
        con.execute(f"INSERT OR REPLACE INTO daily SELECT * FROM src.daily WHERE code IN ({','.join('?' * len(keep))})", keep)
        con.execute("INSERT OR REPLACE INTO daily SELECT * FROM src.daily WHERE date >= ?", (snap_start,))
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        con.execute("DETACH DATABASE src")
        con.close()
        big.unlink()
        print(f"  保留 {n:,} 列", flush=True)

    REPORTS.mkdir(exist_ok=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    failed = []
    for code, name, _ in targets:
        out = REPORTS / f"{code.replace('.', '')}_{latest}.md"
        print(f"\n===== REPORT {code} {name} =====", flush=True)
        try:
            analyze.main([code, "--dir", str(OUT), "--years", str(years), "--capital", capital, "--out", str(out)])
        except SystemExit as e:
            print(f"{code} 分析失敗：{e}", flush=True)
            failed.append(code)
            continue
        print(f"===== END {code} =====", flush=True)
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(out.read_text(encoding="utf-8") + "\n\n---\n\n")
    if failed:
        sys.exit("分析失敗：" + "、".join(failed))


if __name__ == "__main__":
    main()
