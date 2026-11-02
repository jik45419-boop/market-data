#!/usr/bin/env python3
"""清理 Drive 上 market-data 資料夾佔用的空間：垃圾桶裡本程式的檔案、重複舊檔、舊版本"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import notice  # noqa: E402
from store import get_store  # noqa: E402

st = get_store()
msg = st.cleanup()
notice("清理 Drive 空間", msg)
if hasattr(st, "d"):
    q = st.d.about().get(fields="storageQuota").execute()["storageQuota"]
    notice("Drive 空間", f"上限 {int(q['limit']) / 1e9:.2f} GB，已用 {int(q['usage']) / 1e9:.2f} GB"
                         f"（垃圾桶 {int(q.get('usageInDriveTrash', 0)) / 1e9:.2f} GB）")
