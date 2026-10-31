#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
步驟 1：準備
- 從 Drive 下載 meta.db（股票清單、進度）
- 更新股票清單、行業、交易日曆、指數成分
- 探測資料源最新日期
"""
from datetime import date

import baostock as bs

import bsapi
from common import (DAILY_START, DATA, META, classify, gunzip_file, notice,
                    now, open_meta, set_state)
from store import get_store


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    store = get_store()
    if store.exists("meta.db.gz"):
        store.download("meta.db.gz", DATA / "meta.db.gz")
        gunzip_file(DATA / "meta.db.gz", META)
        (DATA / "meta.db.gz").unlink()
        print("已下載 meta.db", flush=True)
    else:
        print("Drive 上還沒有 meta.db，建立新的", flush=True)
    conn = open_meta()

    bsapi.login()
    try:
        # 股票清單
        fields, rows = bsapi.query(bs.query_stock_basic)
        f = {n: i for i, n in enumerate(fields)}
        stamp = now()
        n = 0
        for r in rows:
            kind = classify(r[f["code"]], r[f["type"]])
            if not kind:
                continue
            out_date = r[f["outDate"]] or None
            if out_date and out_date < DAILY_START:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO securities VALUES(?,?,?,?,?,?,?)",
                (r[f["code"]], r[f["code_name"]], kind, int(r[f["status"]] or 0),
                 r[f["ipoDate"]] or None, out_date, stamp))
            n += 1
        counts = dict(conn.execute(
            "SELECT kind || CASE status WHEN 1 THEN '上市' ELSE '退市' END, COUNT(*) "
            "FROM securities GROUP BY 1").fetchall())
        notice("股票清單", f"{n} 檔：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))

        # 行業分類
        try:
            fields, rows = bsapi.query(bs.query_stock_industry)
            f = {x: i for i, x in enumerate(fields)}
            conn.execute("DELETE FROM industry")
            conn.executemany("INSERT OR REPLACE INTO industry VALUES(?,?,?,?)", [
                (r[f["code"]], r[f["industry"]], r[f["industryClassification"]],
                 r[f["updateDate"]]) for r in rows])
        except Exception as e:
            notice("行業分類", f"更新失敗，保留舊資料：{e}", "warning")

        # 交易日曆
        try:
            _, rows = bsapi.query(bs.query_trade_dates, start_date=DAILY_START,
                                  end_date=f"{date.today().year}-12-31")
            conn.executemany("INSERT OR REPLACE INTO trade_dates VALUES(?,?)",
                             [(r[0], int(r[1])) for r in rows])
        except Exception as e:
            notice("交易日曆", f"更新失敗：{e}", "warning")

        # 指數成分
        for name, fn in (("hs300", bs.query_hs300_stocks),
                         ("zz500", bs.query_zz500_stocks),
                         ("sz50", bs.query_sz50_stocks)):
            try:
                fields, rows = bsapi.query(fn)
                f = {x: i for i, x in enumerate(fields)}
                if rows:
                    conn.execute("DELETE FROM index_members WHERE index_name=?", (name,))
                    conn.executemany("INSERT INTO index_members VALUES(?,?,?,?)", [
                        (name, r[f["code"]], r[f["code_name"]], r[f["updateDate"]])
                        for r in rows])
            except Exception as e:
                notice("指數成分", f"{name} 更新失敗：{e}", "warning")

        # 資料源最新日期（用流動性高的股票探測）
        dd = bsapi.last_date("sh.600000", "d") or bsapi.last_date("sz.000001", "d")
        md = bsapi.last_date("sh.600000", "15") or bsapi.last_date("sz.000001", "15")
        if not dd:
            raise RuntimeError("無法取得資料源最新日期")
        set_state(conn, "daily_data_date", dd)
        set_state(conn, "m15_data_date", md or dd)
        notice("資料源最新日期", f"日線 {dd}，15分鐘線 {md}")
    finally:
        bsapi.logout()

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
