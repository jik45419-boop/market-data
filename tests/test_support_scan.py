#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""支撐掃描的型態判斷測試（不連網）：python tests/test_support_scan.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from support_scan import retests, support_band, zigzag  # noqa: E402


def path(points, steps=60):
    """把轉折價位用直線接起來"""
    out = []
    for a, b in zip(points, points[1:]):
        out += [a + (b - a) * i / steps for i in range(steps)]
    return out + [points[-1]]


# 在 10 元附近觸底 5 次、每次彈到 13~15 元，最後又跌回 10.5
box = path([14, 10, 14, 10.2, 13, 9.9, 15, 10.4, 13.5, 10.1, 14, 10.5])
piv, d, _ = zigzag(box)
assert [t for t, _ in piv] == ["H", "L"] * 5 + ["H"], piv
assert d == -1
b = support_band(box)
assert len(b["touches"]) == 5 and abs(b["lo"] - 9.9) < 1e-9 and abs(b["hi"] - 10.4) < 1e-9, b
assert all(x > 0.25 for x in b["bounces"])
assert retests(box) == [(b["touches"][3], True), (b["touches"][4], True)]

# 三次守住之後跌破：第四個低點算一次「沒守住」，而且不再是有效支撐帶
broke = path([14, 10, 14, 10.2, 13, 9.9, 13, 7, 9])
r = retests(broke)
assert len(r) == 1 and r[0][1] is False, r
assert len(support_band(broke)["touches"]) == 1      # 只剩 7 元那個新低

# 一路上漲沒有回落：只有起點一個低點，達不到名單要求的觸底次數
assert len(support_band(path([10, 30]))["touches"]) == 1
print("支撐掃描測試通過 ✅")
