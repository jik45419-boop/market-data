#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市場即時新聞：每分鐘抓一次東方財富 7x24 快訊、每 5 分鐘抓一次滬深全市場公告，存到 Drive 的 market-data 資料夾。
（只抓公開的全市場資訊，不含任何持股或策略設定）

Drive 上的檔案
  news_live.txt               最新的快訊與公告（JSON 文字，原地覆蓋、檔案 ID 固定），有新內容才更新，最快每分鐘一次
  news/news_YYYY-MM-DD.csv    「news」子資料夾：每天一個 CSV，當天抓到的全部內容（一列一則），永久保存、不會自動刪；
                              每 10 分鐘與結束時存檔；重新啟動會接著當天的檔案繼續。Excel、Google 試算表可以直接開

執行方式
  GitHub 的排程不準時，所以一次執行會連續跑幾個小時（RUN_MIN），由排程接力；同一時間只會有一個在跑。
  北京時間 0～7 點不抓（NIGHT_OFF=0 可關掉這個限制）。

用法：python news_live.py           環境變數 RUN_MIN（預設 345）、POLL_SEC（預設 60）、LOCAL_STORE（本地測試）
"""
import csv
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from store import get_store

RUN_MIN = float(os.environ.get("RUN_MIN", "345"))
POLL_SEC = float(os.environ.get("POLL_SEC", "60"))
ANN_EVERY = 5            # 每幾輪抓一次公告
SAVE_EVERY = 10          # 每幾輪存一次當天存檔
LIVE_FAST, LIVE_ANN = 60, 150
SUM_LEN = 140
NIGHT_OFF = os.environ.get("NIGHT_OFF", "1") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
WORK = Path(os.environ.get("DATA_DIR", "data"))
ARCHIVE_DIR = "news"
CSV_HEAD = ["時間", "類型", "標題", "摘要", "股票代碼", "股票名稱", "相關代碼", "連結", "編號"]
CSV_KEYS = ["t", "kind", "title", "sum", "code", "name", "stocks", "url", "id"]


def now_bj():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def http(url, referer=None):
    req = urllib.request.Request(url, headers=dict(UA, **({"Referer": referer} if referer else {})))
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))


def clean(s, n=None):
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(s or ""))).replace("\\", "").strip()
    return s if not n or len(s) <= n else s[:n] + "…"


def fetch_fast():
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd="
           f"&pageSize=50&req_trace={int(time.time() * 1000)}")
    out = []
    for x in (http(url, "https://kuaixun.eastmoney.com/").get("data") or {}).get("fastNewsList") or []:
        code = str(x.get("code") or "")
        if not code:
            continue
        out.append(dict(id="f" + code, kind="快訊", t=str(x.get("showTime", ""))[:19], title=clean(x.get("title")) or clean(x.get("summary"), 40),
                        sum=clean(x.get("summary"), SUM_LEN), stocks=[str(s) for s in x.get("stockList") or []][:8],
                        url=f"https://finance.eastmoney.com/a/{code}.html"))
    return out


def fetch_ann():
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=100&page_index=1&ann_type=SHA,SZA&client_source=web"
    out = []
    for x in (http(url).get("data") or {}).get("list") or []:
        art, codes = str(x.get("art_code") or ""), x.get("codes") or []
        if not art or not codes:
            continue
        c = codes[0]
        out.append(dict(id="a" + art, kind="公告", t=str(x.get("display_time") or "")[:19], title=clean(x.get("title")),
                        code=str(c.get("stock_code", "")), name=clean(c.get("short_name")),
                        col="、".join(k.get("column_name", "") for k in x.get("columns") or []),
                        url=f"https://data.eastmoney.com/notices/detail/{c.get('stock_code')}/{art}.html"))
    return out


class Day:
    """當天的存檔（CSV，一列一則）"""

    def __init__(self, store, day):
        self.store, self.day, self.items, self.dirty = store, day, {}, False
        self.name = f"news_{day}.csv"
        WORK.mkdir(parents=True, exist_ok=True)
        self.path = WORK / self.name
        if store.exists(self.name):
            try:
                store.download(self.name, self.path)
                with open(self.path, encoding="utf-8-sig", newline="") as fh:
                    for row in list(csv.reader(fh))[1:]:
                        it = dict(zip(CSV_KEYS, row))
                        it["stocks"] = [x for x in it.get("stocks", "").split(" ") if x]
                        if it.get("id"):
                            self.items[it["id"]] = it
            except Exception as e:      # noqa: BLE001  壞檔就從空的開始，新的會覆蓋它
                print(f"讀不到 {self.name}（{type(e).__name__}: {e}），從空的開始", flush=True)

    def add(self, it):
        if it["id"] in self.items:
            return False
        self.items[it["id"]] = it
        self.dirty = True
        return True

    def save(self):
        if not self.dirty:
            return
        with open(self.path, "w", encoding="utf-8-sig", newline="") as fh:      # 帶 BOM：Excel 直接開不會亂碼
            w = csv.writer(fh)
            w.writerow(CSV_HEAD)
            for it in sorted(self.items.values(), key=lambda x: x["t"]):
                w.writerow([" ".join(it.get(k) or []) if k == "stocks" else ((it.get("col") or it.get("sum", "")) if k == "sum" else it.get(k, ""))
                            for k in CSV_KEYS])
        self.store.upload(self.path, self.name, mimetype="text/csv")
        self.dirty = False
        print(f"已存檔 {ARCHIVE_DIR}/{self.name}（{len(self.items)} 則）", flush=True)


def live_text(fast, ann, errors):
    top = lambda d, n: sorted(d.values(), key=lambda x: x["t"], reverse=True)[:n]      # noqa: E731
    slim = lambda it: {k: v for k, v in it.items() if k not in ("id", "kind") and v not in ("", [])}      # noqa: E731
    return json.dumps(dict(schema=1, kind="news_live", updated=now_bj().strftime("%Y-%m-%d %H:%M:%S"), errors=errors,
                           fast=[slim(x) for x in top(fast, LIVE_FAST)], ann=[slim(x) for x in top(ann, LIVE_ANN)]),
                      ensure_ascii=False, separators=(",", ":"))


def main():
    store = get_store()
    t0, loop, n_new, n_put = time.time(), 0, 0, 0
    arch = store.sub(ARCHIVE_DIR)
    store.delete("news_2026-11-14.jsonl.gz")      # 第一版測試時留在外層的舊格式存檔
    day = Day(arch, now_bj().strftime("%Y-%m-%d"))
    fast = {k: v for k, v in day.items.items() if v["kind"] == "快訊"}
    ann = {k: v for k, v in day.items.items() if v["kind"] == "公告"}
    print(f"開始：今天已有 {len(day.items)} 則，最多跑 {RUN_MIN:.0f} 分鐘，每 {POLL_SEC:.0f} 秒一輪", flush=True)
    last_err = {}
    while (time.time() - t0) / 60 < RUN_MIN:
        bj = now_bj()
        if NIGHT_OFF and bj.hour < 7 and loop > 0:
            print("北京時間 0～7 點不抓，結束", flush=True)
            break
        today = bj.strftime("%Y-%m-%d")
        if today != day.day:                       # 換日：存好昨天的，開新的一天
            day.save()
            day = Day(arch, today)
        changed = False
        jobs = [("快訊", fetch_fast, fast)] + ([("公告", fetch_ann, ann)] if loop % ANN_EVERY == 0 else [])
        for name, fn, pool in jobs:
            try:
                for it in fn():
                    if it["id"] not in pool:
                        pool[it["id"]] = it
                        changed = True
                        n_new += day.add(it)
                last_err.pop(name, None)
            except Exception as e:      # noqa: BLE001  抓不到就等下一輪
                last_err[name] = f"{name}：{type(e).__name__} {str(e)[:100]}（{bj:%H:%M}）"
                print(last_err[name], flush=True)
        for pool, cap in ((fast, LIVE_FAST * 3), (ann, LIVE_ANN * 3)):      # 記憶體裡只留最近的
            if len(pool) > cap:
                for k in sorted(pool, key=lambda k: pool[k]["t"])[:len(pool) - cap]:
                    del pool[k]
        try:
            if changed or loop == 0:
                store.put_text("news_live.txt", live_text(fast, ann, list(last_err.values())))
                n_put += 1
            if loop % SAVE_EVERY == SAVE_EVERY - 1:
                day.save()
        except Exception as e:      # noqa: BLE001  Drive 一時連不上：下一輪再存
            print(f"Drive 寫入失敗：{type(e).__name__} {str(e)[:150]}", flush=True)
        loop += 1
        time.sleep(max(1.0, POLL_SEC - (time.time() - t0) % POLL_SEC))
    day.save()
    msg = f"跑了 {loop} 輪、{(time.time() - t0) / 60:.0f} 分鐘；新增 {n_new} 則；更新 news_live.txt {n_put} 次；今天累計 {len(day.items)} 則"
    print(msg, flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=即時新聞::{msg}", flush=True)
        if last_err:
            print(f"::warning title=即時新聞::最後仍抓不到：{'；'.join(last_err.values())[:800]}", flush=True)
    if loop and not fast and not ann:
        sys.exit("整段時間一則都沒抓到")


if __name__ == "__main__":
    main()
