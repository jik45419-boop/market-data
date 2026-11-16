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
  GitHub 的排程不準時、偶爾還會漏掉一次，所以一次執行會連續跑幾個小時（RUN_MIN），由排程接力；同一時間只會有一個在跑。
  北京時間 0～7 點不抓（NIGHT_OFF=0 可關掉這個限制）。
  補抓：每次啟動都往回翻頁，把最近 26 小時內還沒存過的補齊（夜間、或排程漏掉的那段）；
        平常每一輪如果整頁都是新的，也會繼續往回翻，直到接上已經有的為止。
  不重複：存過的不再存（比對當天與前一天的檔案）；比前一天更早的內容不收。

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
BACK_HOURS = 26                       # 啟動時往回補抓幾小時
PAGES_START = dict(快訊=80, 公告=120)   # 啟動補抓最多翻幾頁（快訊每頁 50 則、公告每頁 100 則）
PAGES_LOOP = dict(快訊=10, 公告=30)     # 平常每一輪最多翻幾頁
NIGHT_OFF = os.environ.get("NIGHT_OFF", "1") == "1"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
WORK = Path(os.environ.get("DATA_DIR", "data"))
ARCHIVE_DIR = "news"
CSV_HEAD = ["時間", "類型", "標題", "摘要", "股票代碼", "股票名稱", "相關代碼", "連結", "編號"]
CSV_KEYS = ["t", "kind", "title", "sum", "code", "name", "stocks", "url", "id"]


def now_bj():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def http(url, referer=None, tries=3):
    req = urllib.request.Request(url, headers=dict(UA, **({"Referer": referer} if referer else {})))
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))
        except Exception:      # noqa: BLE001  連線偶爾被重設：等一下再試
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def clean(s, n=None):
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(s or ""))).replace("\\", "").strip()
    return s if not n or len(s) <= n else s[:n] + "…"


def fast_pages(max_pages):
    """7x24 快訊，一頁一頁往回翻（新 → 舊）"""
    sort_end = ""
    for _ in range(max_pages):
        url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102"
               f"&sortEnd={sort_end}&pageSize=50&req_trace={int(time.time() * 1000)}")
        data = http(url, "https://kuaixun.eastmoney.com/").get("data") or {}
        out = []
        for x in data.get("fastNewsList") or []:
            code = str(x.get("code") or "")
            if not code:
                continue
            out.append(dict(id="f" + code, kind="快訊", t=str(x.get("showTime", ""))[:19], title=clean(x.get("title")) or clean(x.get("summary"), 40),
                            sum=clean(x.get("summary"), SUM_LEN), stocks=[str(s) for s in x.get("stockList") or []][:8],
                            url=f"https://finance.eastmoney.com/a/{code}.html"))
        if not out:
            return
        yield out
        sort_end = str(data.get("sortEnd") or "")
        if not sort_end:
            return


def ann_pages(max_pages):
    """滬深全市場公告，一頁一頁往回翻（新 → 舊）"""
    for page in range(1, max_pages + 1):
        url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=100"
               f"&page_index={page}&ann_type=SHA,SZA&client_source=web")
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
        if not out:
            return
        yield out


def pull(pages, pool, floor, deep):
    """
    往回翻頁收集 pool 裡還沒有的。停止條件：翻到比 floor 更早；或（平常）這一頁裡已經有存過的＝接上了。
    deep=True（啟動時）不管有沒有接上，一律翻到 floor。中途某一頁抓不到：保留已經拿到的，例外往上丟給下一輪處理前先回傳
    """
    got, err = [], None
    try:
        for items in pages:
            fresh = [it for it in items if it["id"] not in pool]
            got += fresh
            if min((it["t"] for it in items if it["t"]), default="") < floor:
                break
            if not deep and len(fresh) < len(items):
                break
            time.sleep(0.25)
    except Exception as e:      # noqa: BLE001
        err = e
    return got, err


class Day:
    """當天的存檔（CSV，一列一則）"""

    def __init__(self, store, day):
        self.store, self.day, self.dirty = store, day, False
        self.name = f"news_{day}.csv"
        WORK.mkdir(parents=True, exist_ok=True)
        self.path = WORK / self.name
        self.items = self._read(self.name)
        # 前一天已經存過的編號：每天第一次啟動會抓到昨晚的內容，不要再存進今天的檔案
        self.prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        self.known = set(self._read(f"news_{self.prev}.csv"))
        old = [k for k, v in self.items.items() if v["t"] and v["t"][:10] < self.prev]      # 舊版程式重複存進來的更早內容：拿掉
        for k in old:
            del self.items[k]
        if old:
            self.dirty = True
            print(f"{self.name}：拿掉 {len(old)} 則比 {self.prev} 更早、以前的檔案已經有的內容", flush=True)

    def _read(self, name):
        out = {}
        if not self.store.exists(name):
            return out
        try:
            path = WORK / name
            self.store.download(name, path)
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for row in list(csv.reader(fh))[1:]:
                    it = dict(zip(CSV_KEYS, row))
                    it["stocks"] = [x for x in it.get("stocks", "").split(" ") if x]
                    if it.get("id"):
                        out[it["id"]] = it
        except Exception as e:      # noqa: BLE001  壞檔就當作沒有，新的會覆蓋它
            print(f"讀不到 {name}（{type(e).__name__}: {e}），當作空的", flush=True)
        return out

    def add(self, it):
        if it["id"] in self.items or it["id"] in self.known:
            return False
        if it["t"] and it["t"][:10] < self.prev:      # 比前一天更早的（例如週一看到的上週五公告）：以前的檔案已經有了
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
        deep = loop == 0
        floor = (bj - timedelta(hours=BACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        jobs = [("快訊", fast_pages, fast)] + ([("公告", ann_pages, ann)] if loop % ANN_EVERY == 0 else [])
        for name, pages, pool in jobs:
            got, err = pull(pages((PAGES_START if deep else PAGES_LOOP)[name]), pool, floor, deep)
            added = 0
            for it in got:
                pool[it["id"]] = it
                changed = True
                added += day.add(it)
            n_new += added
            if deep or len(got) > 60:
                print(f"{bj:%H:%M} {name}：這一輪拿到 {len(got)} 則沒看過的，存進今天的檔案 {added} 則", flush=True)
            if err is None:
                last_err.pop(name, None)
            else:                                   # 抓不到就等下一輪
                last_err[name] = f"{name}：{type(err).__name__} {str(err)[:100]}（{bj:%H:%M}）"
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
