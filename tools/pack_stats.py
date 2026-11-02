"""列出 Drive 上年度資料庫的大小（只讀）"""
import sqlite3, sys
sys.path.insert(0, ".")
from common import DATA, gunzip_file
from store import get_store

DATA.mkdir(parents=True, exist_ok=True)
st = get_store()
st.download("meta.db.gz", DATA / "meta.db.gz")
gunzip_file(DATA / "meta.db.gz", DATA / "meta.db")
c = sqlite3.connect(DATA / "meta.db")
rows = c.execute("SELECT name, dataset, rows, size_mb, size_gz_mb FROM packs ORDER BY name").fetchall()
files = sorted(n for n in st.files if ".db.gz" in n)
print(f"::notice title=總覽::{len(rows)} 個資料庫、Drive 上 {len(files)} 個檔（含 meta）；解壓後合計 {sum(r[3] for r in rows):,.0f} MB，"
      f"壓縮後 {sum(r[4] for r in rows):,.0f} MB")
print("::notice title=各資料庫::" + "；".join(f"{r[0]}: {r[1]}｜解壓 {r[3]:.0f}MB、壓縮 {r[4]:.0f}MB" for r in rows))

# Drive 空間與舊版本
q = st.d.about().get(fields="storageQuota").execute()["storageQuota"]
gb = lambda k: f"{int(q.get(k, 0)) / 1e9:.2f} GB"
print(f"::notice title=Drive 空間::上限 {gb('limit')}，已用 {gb('usage')}（Drive {gb('usageInDrive')}，垃圾桶 {gb('usageInDriveTrash')}）")
info = []
for n in sorted(st.files)[:400]:
    revs = st.d.revisions().list(fileId=st.files[n], fields="revisions(id,size,keepForever)").execute().get("revisions", [])
    if len(revs) > 1:
        info.append(f"{n} {len(revs)} 版 {sum(int(r.get('size', 0)) for r in revs) / 1e6:.0f}MB")
print("::notice title=舊版本::" + ("；".join(info[:40]) if info else "沒有檔案保留舊版本"))
