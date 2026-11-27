#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""資料品質：完全重複列、預告列、波指非交易日、公布日（來源可信才用、日頻產業指標當天可用）、自動品質檢查"""
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import macro  # noqa: E402

tmp = Path(tempfile.mkdtemp())
con = macro.open_db(tmp / "m.db")

# 1 完全重複列只留一筆
macro.write_full(con, "buffett_index", pd.DataFrame({"日期": ["2019-05-09"] * 3 + ["2019-05-10"], "收盘价": [1, 1, 1, 2]}))
assert con.execute("select count(*) from buffett_index").fetchone()[0] == 2

# 2 預告列（現值空）不收
macro.write_full(con, "japan_cpi", pd.DataFrame({"时间": ["2026年10月", "2026年11月"], "前值": [1.0, 1.1],
                                                "现值": [1.1, None], "发布日期": ["2026-11-20", "2026-12-18"]}))
assert [r[0] for r in con.execute("select 期間 from japan_cpi")] == ["2026-10"]
# 來源公布日合理（期間後 200 天內）→ 直接採用；日期 2026-11-20 不是規則估的 2026-11-25
assert con.execute('select "公布日(估)" from japan_cpi').fetchone()[0] == "2026-11-20"

# 3 來源公布日不合理（整欄同一天）→ 退回規則估計
macro.write_full(con, "us_cpi_yoy", pd.DataFrame({"时间": ["2008-04-01", "2008-05-01"], "前值": [1, 1], "现值": [1, 2],
                                                 "发布日期": ["2025-06-17", "2025-06-17"]}))
assert [r[0] for r in con.execute('select "公布日(估)" from us_cpi_yoy order by 期間')] == ["2008-05-15", "2008-06-15"], \
    con.execute('select * from us_cpi_yoy').fetchall()

# 4 產業指標：日頻當天可用、月頻套規則
days = [(date(2026, 11, 26) - timedelta(days=i)).isoformat() for i in range(300)]
d = pd.DataFrame({"日期": days, "指标": "波罗的海干散货指数", "数值": 1.0})
m = pd.DataFrame({"日期": ["2026-09-01", "2026-10-01"], "指标": "铁路货运量:当月值", "数值": 2.0})
macro.write_full(con, "industry_indicators", pd.concat([d, m], ignore_index=True))
r = dict(con.execute('select 期間, "可用日" from industry_indicators where 指标 in ("波罗的海干散货指数") limit 3').fetchall())
assert all(k == v for k, v in r.items()), r
assert con.execute('select "可用日" from industry_indicators where 指标="铁路货运量:当月值" and 期間="2026-10-01"').fetchone()[0] == "2026-11-25"

# 5 波指：去非交易日、同日只留最後一筆
q = pd.DataFrame({"品種": ["50ETF"] * 4, "date": ["2026-10-30", "2026-11-01", "2026-11-02", "2026-11-02"],
                  "close": [1.0, 2.0, 3.0, 4.0]})
out = macro.clean_qvix(q, ["2026-10-30", "2026-11-02"])
assert out["date"].tolist() == ["2026-10-30", "2026-11-02"] and out["close"].tolist() == [1.0, 4.0]

# 6 品質檢查：能抓到重複列與未來期間
con.execute("create table bad(期間 text, v real)")
con.executemany("insert into bad values(?,?)", [("2026-01-01", 1), ("2026-01-01", 1), ("2099-01-01", 2), ("2099-01", 3)])
con.commit()
rep = {(t, i): n for t, i, n in macro.quality_report(con)}
assert rep.get(("bad", "完全重複的列")) == 1 and rep.get(("bad", "期間在未來")) == 2, rep
assert not [k for k in rep if k[0] in ("buffett_index", "japan_cpi", "industry_indicators")], rep
# 7 季度寫法「2008第1季度」（沒有「年」）要認得；期間是空的要被品質檢查抓到
assert macro.norm_period("2008第1季度") == "2008Q1" and macro.norm_period("2026年第1-3季度") == "2026Q3"
con.execute("create table bad2(期間 text, v real)")
con.executemany("insert into bad2 values(?,?)", [(None, 1), ("2026-01-01", 2)])
con.commit()
assert ("bad2", "期間是空的（日期格式沒認出來）", 1) in macro.quality_report(con)
# 8 逐期資料：來源沒資料的年份（akshare 丟 TypeError）要當成空的、標成完成，不要永遠重試
def _src(y):
    if int(y) < 2010:
        raise TypeError("no data")
    return pd.DataFrame({"日期": [f"{y}-06-01"], "v": [1]})


ds = ("t_year", "測試", "分類", "來源", ("year", "2008", _src, 1), "")
macro.run_periodic(con, ds)
done = {r[0] for r in con.execute("select period from _progress where id='t_year'")}
assert {"2008", "2009", "2010"} <= done, done
print("資料品質測試通過")
