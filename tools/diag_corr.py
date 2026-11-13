#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""資料診斷：個股與各指數的逐年日報酬相關係數、最近幾天收盤價（讀 analyze_ci.py 產生的精簡資料庫）"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402

INDEXES = ["sh.000300", "sh.000016", "sh.000001", "sz.399001", "sz.399006"]


def main():
    data = analyze.Data(os.environ.get("ANALYZE_DIR", "analyze_data"))
    codes = [analyze.resolve(data, c)[0] for c in os.environ["CODES"].replace("，", ",").split(",") if c.strip()]
    df = data.daily(codes + INDEXES, "2021-01-01", "2099-12-31", "code, date, close, adj_factor, volume")
    df["px"] = df.close * df.adj_factor.fillna(1.0)
    px = df.pivot(index="date", columns="code", values="px").sort_index()
    ret = np.log(px).diff()
    print("\n===== DIAG 各代碼資料筆數與日期範圍 =====")
    for c in codes + INDEXES:
        s = px[c].dropna() if c in px else pd.Series(dtype=float)
        print(f"{c}: {len(s)} 筆，{s.index.min() if len(s) else '-'} ~ {s.index.max() if len(s) else '-'}")
    print("\n===== DIAG 逐年日報酬相關係數 =====")
    years = sorted({d[:4] for d in ret.index})
    print("代碼 vs 指數 | " + " | ".join(years))
    for c in codes:
        for i in INDEXES:
            if c not in ret or i not in ret:
                continue
            row = []
            for y in years:
                sub = ret.loc[[d for d in ret.index if d.startswith(y)], [c, i]].dropna()
                row.append(f"{sub[c].corr(sub[i]):.2f}" if len(sub) > 20 else "-")
            print(f"{c} vs {i} | " + " | ".join(row))
    print("\n===== DIAG 最近 12 天收盤（不復權） =====")
    raw = df.pivot(index="date", columns="code", values="close").sort_index().tail(12)
    print(raw.to_string())
    print("\n===== DIAG 2024-10 前後 5 天（滬深300 大漲期間）=====")
    print(df.pivot(index="date", columns="code", values="close").loc["2024-09-25":"2024-10-10"].to_string())


if __name__ == "__main__":
    main()
