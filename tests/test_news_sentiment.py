#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新聞情緒：用假的每日新聞檔驗證利多／利空判斷、增減持回購計數、重算最近幾天"""
import csv
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import news_sentiment as ns  # noqa: E402
from store import LocalStore  # noqa: E402

tmp = Path(tempfile.mkdtemp())
store = LocalStore(tmp / "drive")
HEAD = ["時間", "類型", "標題", "摘要", "股票代碼", "股票名稱", "相關代碼", "連結", "編號"]


def write(day, rows):
    d = tmp / "drive" / "news"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"news_{day}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEAD)
        for i, (k, t, s) in enumerate(rows):
            w.writerow([f"{day} 10:00", k, t, s, "", "", "", "", f"{day}-{i}"])


write("2026-11-25", [("快訊", "央行宣布降准", "市场大涨"), ("快訊", "某公司股价跌停", "遭立案调查"),
                     ("快訊", "国务院常务会议召开", ""), ("公告", "关于控股股东增持股份的公告", ""),
                     ("公告", "关于股东减持计划的公告", ""), ("公告", "关于回购股份进展的公告", "")])
write("2026-11-26", [("快訊", "A股三大指数集体上涨", "两市成交额突破2万亿"), ("快訊", "美股大跌", "")])
con = sqlite3.connect(tmp / "m.db")
assert ns.update(store, con, tmp / "w", log=lambda *_: None) == 2
r = {row[0]: row for row in con.execute('select 期間, 快讯则数, 利多则数, 利空则数, 中性则数, 情绪指数, 公告则数, 增持公告, 减持公告, 回购公告 from news_sentiment')}
assert r["2026-11-25"] == ("2026-11-25", 3, 1, 1, 1, 0.0, 3, 1, 1, 1), r["2026-11-25"]
assert r["2026-11-26"][2:4] == (1, 1), r["2026-11-26"]
# 第二次：最近 2 天重算，不會重複
write("2026-11-26", [("快訊", "A股三大指数集体上涨", ""), ("快訊", "美股大跌", ""), ("快訊", "港股大涨", "")])
ns.update(store, con, tmp / "w", log=lambda *_: None)
assert con.execute("select count(*) from news_sentiment").fetchone()[0] == 2
assert con.execute("select 利多则数 from news_sentiment where 期間='2026-11-26'").fetchone()[0] == 2
print("新聞情緒測試通過")
