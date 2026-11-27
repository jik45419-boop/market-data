# -*- coding: utf-8 -*-
"""GitHub Actions 用：從 Drive 下載 macro.db.gz → 更新 → 上傳 macro.db.gz 和 Excel（股市相关数据_历年.xlsx）"""
import os
import sqlite3
import sys
from pathlib import Path

import breadth
import news_sentiment
import macro
from common import gunzip_file, gzip_file, notice
from store import get_store

DATA = macro.DATA
DB = macro.DB
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    store = get_store()
    gz = DATA / "macro.db.gz"
    if "macro.db.gz" in store.files:
        store.download("macro.db.gz", gz)
        gunzip_file(gz, DB)
        print("已下載上次的 macro.db", flush=True)
    else:
        print("Drive 上還沒有 macro.db，從頭抓", flush=True)

    only = {x for x in os.environ.get("ONLY", "").split(",") if x.strip()} or None
    ok, bad = macro.run(only, budget_min=float(os.environ.get("BUDGET_MIN", "300")))

    # 市場寬度：用同一個 Drive 資料夾裡的 A 股日線數據包計算（第一次要下載全部數據包，之後只讀最近 15 個月）
    if not only or "market_breadth" in only:
        con = macro.open_db(DB)
        try:
            n = breadth.update(store, con, DATA / "packs", log=macro.log,
                               max_minutes=float(os.environ.get("BREADTH_MIN", "120")))
            macro.mark(con, "market_breadth", "市场宽度（涨跌停家数、站上均线比例、新高新低、腾落线）", "估值情绪",
                       "用 A 股日线数据包自行计算", "自算", "沪深主板＋创业板；漲跌停按主板 10%、ST 5%、创业板 2020-08-24 起 20%", True)
            macro.log(f"市場寬度：新增 {n} 天")
        except Exception as e:  # noqa: BLE001
            macro.mark(con, "market_breadth", "市场宽度（涨跌停家数、站上均线比例、新高新低、腾落线）", "估值情绪",
                       "用 A 股日线数据包自行计算", "自算", "", False, f"{type(e).__name__}: {str(e)[:200]}")
            macro.log(f"市場寬度失敗：{type(e).__name__} {e}")
        con.close()

    # 新聞情緒：用 news 資料夾裡 news_live.py 存的每日新聞計算
    if not only or "news_sentiment" in only:
        con = macro.open_db(DB)
        name = "新闻情绪（利多／利空则数、情绪指数、增持减持回购公告数）"
        try:
            news_sentiment.update(store, con, DATA / "news", log=macro.log)
            macro.mark(con, "news_sentiment", name, "估值情绪", "用 news_live 存的每日新闻自行计算", "自算",
                       "关键字判断，只是粗略的情绪温度计；2026-11 开始有新闻存档", True)
        except Exception as e:  # noqa: BLE001
            macro.mark(con, "news_sentiment", name, "估值情绪", "用 news_live 存的每日新闻自行计算", "自算", "", False,
                       f"{type(e).__name__}: {str(e)[:200]}")
            macro.log(f"新聞情緒失敗：{type(e).__name__} {e}")
        con.close()

    gzip_file(DB, gz)
    store.upload(gz, "macro.db.gz")
    out = DATA / "export"
    path, skipped = macro.export(DB, out)
    store.upload(path, path.name, mimetype=XLSX_MIME)
    for sid, _ in skipped:            # 太大放不進 Excel 的表，另存 CSV
        f = out / f"{sid}.csv.gz"
        store.upload(f, f.name)

    con = sqlite3.connect(str(DB))
    errs = con.execute("select id, name, last_error from _datasets where last_error is not null").fetchall()
    con.close()
    msg = f"成功 {ok} 種、失敗 {bad} 種；已上傳 macro.db.gz 與 {path.name}"
    if errs:
        msg += "。這次有錯誤：" + "、".join(f"{n}" for _, n, _ in errs[:15])
    notice("股市相關數據", msg)
    sys.exit(0)        # 個別來源失敗不讓流程變紅燈（錯誤記在 _datasets.last_error）


if __name__ == "__main__":
    main()
