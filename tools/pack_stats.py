"""列出 Drive 上數據包的大小統計（只讀）"""
import sqlite3, sys, collections
sys.path.insert(0, ".")
from common import DATA, gunzip_file
from store import get_store

DATA.mkdir(parents=True, exist_ok=True)
st = get_store()
st.download("meta.db.gz", DATA / "meta.db.gz")
gunzip_file(DATA / "meta.db.gz", DATA / "meta.db")
c = sqlite3.connect(DATA / "meta.db")
rows = c.execute("SELECT name, dataset, rows, codes, first_key, last_key, size_mb, size_gz_mb FROM packs ORDER BY name").fetchall()
by = collections.defaultdict(list)
for r in rows:
    kind = r[0].rsplit("_", 1)[0] if r[0].startswith("daily") else r[0][:3]
    by[kind].append(r)
# 15 分鐘線每月大小，以及兩個月合併後的大小
m = {r[0][4:]: r[6] for r in rows if r[0].startswith("m15_")}
pairs = []
for y in sorted({k[:4] for k in m}):
    for a in range(1, 13, 2):
        k1, k2 = f"{y}-{a:02d}", f"{y}-{a + 1:02d}"
        if k1 in m or k2 in m:
            pairs.append((f"{k1}+{a + 1:02d}", m.get(k1, 0) + m.get(k2, 0)))
print(f"::notice title=m15 每月::" + "，".join(f"{k} {v:.0f}" for k, v in sorted(m.items())))
print(f"::notice title=m15 兩個月一包::最大 {max(p[1] for p in pairs):.0f} MB；超過 190MB 的：" + "，".join(f"{k} {v:.0f}" for k, v in pairs if v > 190))

tot_s = sum(r[6] for r in rows); tot_g = sum(r[7] for r in rows)
print(f"::notice title=總覽::{len(rows)} 個數據包，解壓後合計 {tot_s:,.0f} MB，壓縮後 {tot_g:,.0f} MB")
for kind, rs in list(by.items())[:6]:
    yr = collections.defaultdict(lambda: [0, 0.0, 0.0, 0])
    for r in rs:
        y = str(r[4])[:4]
        yr[y][0] += 1; yr[y][1] += r[6]; yr[y][2] += r[7]; yr[y][3] += r[2]
    print(f"::notice title={kind}（{len(rs)} 包）::每包解壓 {min(r[6] for r in rs):.0f}～{max(r[6] for r in rs):.0f} MB；"
          + "；".join(f"{y}: {v[0]} 包 {v[1]:.0f}MB/壓縮 {v[2]:.0f}MB/{v[3]:,} 列" for y, v in sorted(yr.items())))

