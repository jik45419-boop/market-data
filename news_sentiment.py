# -*- coding: utf-8 -*-
"""
新聞情緒指標：用 news_live.py 存在 Drive「news」資料夾的每日新聞（news_YYYY-MM-DD.csv）計算
  每天：快訊則數、利多則數、利空則數、情緒指數＝(利多－利空)／(利多＋利空)（－1～＋1）
        公告則數、增持／減持／回購公告則數
  判斷方式：標題＋摘要裡「利多關鍵字」出現次數多於「利空關鍵字」＝利多，反之＝利空，一樣多＝中性
  （關鍵字是簡體，因為新聞來源是簡體）
  結果寫進 macro.db 的 news_sentiment 表；每天只算還沒算過的日子，最近 2 天每次重算（當天的檔案會一直變）
"""
import csv
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

TABLE = "news_sentiment"
NAME_RE = re.compile(r"^news_(\d{4}-\d{2}-\d{2})\.csv$")

POS = ("利好", "上涨", "大涨", "涨停", "增持", "回购", "降准", "降息", "刺激", "超预期", "突破", "新高", "增长", "盈利",
       "反弹", "拉升", "走高", "提振", "宽松", "净买入", "净流入", "中标", "签约", "获批", "扭亏", "上调", "利多", "企稳")
NEG = ("利空", "下跌", "大跌", "跌停", "减持", "暴跌", "下滑", "亏损", "违约", "处罚", "立案", "风险", "警示", "制裁",
       "关税", "加息", "收紧", "走低", "跳水", "净卖出", "净流出", "退市", "爆雷", "不及预期", "下调", "冲突", "衰退", "暴雷")


def tone(text):
    """回傳 1 利多、-1 利空、0 中性"""
    p = sum(text.count(k) for k in POS)
    n = sum(text.count(k) for k in NEG)
    return 1 if p > n else (-1 if n > p else 0)


def day_stats(day, rows):
    """rows：[(類型, 標題, 摘要)]"""
    fast = [(t or "") + " " + (s or "") for k, t, s in rows if k != "公告" and "公告" not in (k or "")]
    ann = [t or "" for k, t, s in rows if k == "公告" or "公告" in (k or "")]
    tones = [tone(x) for x in fast]
    pos, neg = tones.count(1), tones.count(-1)
    return {"期間": day, "快讯则数": len(fast), "利多则数": pos, "利空则数": neg, "中性则数": tones.count(0),
            "情绪指数": round((pos - neg) / (pos + neg), 4) if pos + neg else None,
            "公告则数": len(ann), "增持公告": sum("增持" in a for a in ann), "减持公告": sum("减持" in a for a in ann),
            "回购公告": sum("回购" in a for a in ann)}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    h = {k: i for i, k in enumerate(rows[0])}
    gi = lambda r, k: r[h[k]] if k in h and h[k] < len(r) else ""      # noqa: E731
    return [(gi(r, "類型"), gi(r, "標題"), gi(r, "摘要")) for r in rows[1:]]


def update(store, con, workdir, log=print, refresh_days=2):
    news = store.sub("news")
    days = sorted(m.group(1) for n in news.files if (m := NAME_RE.match(n)))
    if not days:
        raise RuntimeError("Drive 的 news 資料夾裡沒有每日新聞檔")
    has = con.execute("select 1 from sqlite_master where type='table' and name=?", (TABLE,)).fetchone()
    done = {r[0] for r in con.execute(f'select "期間" from {TABLE}')} if has else set()
    todo = [d for d in days if d not in done] + [d for d in days[-refresh_days:] if d in done]
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = []
    for d in todo:
        p = workdir / f"news_{d}.csv"
        news.download(f"news_{d}.csv", p)
        out.append(day_stats(d, read_csv(p)))
        p.unlink()
        time.sleep(0.2)
    if not out:
        return 0
    if has:
        con.executemany(f'delete from {TABLE} where "期間"=?', [(r["期間"],) for r in out])
    pd.DataFrame(out).to_sql(TABLE, con, index=False, if_exists="append")
    con.commit()
    log(f"新聞情緒：算了 {len(out)} 天（{out[0]['期間']}～{out[-1]['期間']}）")
    return len(out)
