# -*- coding: utf-8 -*-
"""GitHub Actions 用：從 Drive 下載 macro.db.gz → 更新 → 上傳 macro.db.gz 和 Excel（股市相关数据_历年.xlsx）"""
import os
import sqlite3
import sys
from pathlib import Path

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
