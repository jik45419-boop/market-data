#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
還原年度資料庫：把同一個資料夾裡的 astock_YYYY.db.gz.part01、part02… 依序接起來並解壓成 astock_YYYY.db
用法（只需要 Python，不用安裝其他套件）：
    python restore.py                 # 還原這個資料夾裡所有年份
    python restore.py 2024 2025       # 只還原指定年份
也可以不用這個程式：
    Mac / Linux：cat astock_2025.db.gz.part* | gunzip > astock_2025.db
    Windows：copy /b astock_2025.db.gz.part01+astock_2025.db.gz.part02+astock_2025.db.gz.part03 astock_2025.db.gz
             再用 7-Zip 解壓 astock_2025.db.gz
"""
import glob
import gzip
import os
import re
import shutil
import sys

folder = os.path.dirname(os.path.abspath(sys.argv[0])) if len(sys.argv) < 2 or not sys.argv[1].isdigit() else os.getcwd()
want = {a for a in sys.argv[1:] if a.isdigit()}
names = sorted({re.sub(r"\.db\.gz(\.part\d+)?$", "", os.path.basename(f))
                for f in glob.glob(os.path.join(folder, "*.db.gz*"))})
for name in names:
    if want and not any(name.endswith(y) for y in want):
        continue
    parts = sorted(glob.glob(os.path.join(folder, f"{name}.db.gz.part*")))
    single = os.path.join(folder, f"{name}.db.gz")
    srcs = parts or ([single] if os.path.exists(single) else [])
    nums = [int(re.search(r"part(\d+)$", p).group(1)) for p in parts]
    if parts and nums != list(range(1, len(parts) + 1)):
        print(f"{name}: 分片不完整（有 {nums}），請把所有 part 都下載到同一個資料夾"); continue
    out = os.path.join(folder, f"{name}.db")
    print(f"還原 {name}（{len(srcs)} 個檔）…", flush=True)

    class Chain:
        def __init__(self, files):
            self.files, self.f = list(files), None

        def read(self, n=-1):
            while True:
                if self.f is None:
                    if not self.files:
                        return b""
                    self.f = open(self.files.pop(0), "rb")
                b = self.f.read(n)
                if b:
                    return b
                self.f.close(); self.f = None

    with gzip.GzipFile(fileobj=Chain(srcs)) as g, open(out, "wb") as o:
        shutil.copyfileobj(g, o, 8 * 1024 * 1024)
    print(f"  → {out}（{os.path.getsize(out) / 1e6:,.0f} MB）")
