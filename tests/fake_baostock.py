# -*- coding: utf-8 -*-
"""本地測試用的假 BaoStock（不連網）"""
import zlib
from datetime import date, timedelta

CALLS = []


class RS:
    def __init__(self, fields, rows, code="0", msg="success"):
        self.fields = fields
        self._rows = rows
        self._i = -1
        self.error_code = code
        self.error_msg = msg

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


class LG:
    error_code = "0"
    error_msg = ""


def login():
    return LG()


def logout():
    return LG()


TODAY = date(2026, 10, 30)
BASIC = [
    # code, name, ipo, out, type, status
    ("sh.600000", "浦发银行", "1999-11-10", "", "1", "1"),
    ("sz.000001", "平安银行", "1991-04-03", "", "1", "1"),
    ("sz.300750", "宁德时代", "2018-06-11", "", "1", "1"),
    ("sh.600001", "已退市A", "1998-01-01", "2021-03-15", "1", "0"),
    ("sh.600002", "早退市B", "1998-01-01", "2010-03-15", "1", "0"),
    ("sh.688981", "中芯国际", "2020-07-16", "", "1", "1"),
    ("bj.830799", "北交所X", "2021-11-15", "", "1", "1"),
    ("sh.510300", "沪深300ETF", "2012-05-28", "", "5", "1"),
    ("sz.159915", "创业板ETF", "2011-12-09", "", "5", "1"),
    ("sh.000300", "沪深300", "2005-04-08", "", "2", "1"),
    ("sh.113001", "可转债", "2010-01-01", "", "4", "1"),
]


def trading_days(start, end):
    d = max(date.fromisoformat(start), date(2015, 1, 1))
    e = min(date.fromisoformat(end), TODAY)
    while d <= e:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def query_stock_basic(code="", code_name=""):
    return RS(["code", "code_name", "ipoDate", "outDate", "type", "status"],
              [list(r) for r in BASIC])


def query_stock_industry():
    return RS(["updateDate", "code", "code_name", "industry", "industryClassification"],
              [["2026-10-26", "sh.600000", "浦发银行", "J66货币金融服务", "证监会行业分类"]])


def query_trade_dates(start_date, end_date):
    rows = []
    d = date.fromisoformat(start_date)
    while d <= date.fromisoformat(end_date):
        rows.append([d.isoformat(), "1" if d.weekday() < 5 and d <= TODAY else "0"])
        d += timedelta(days=1)
    return RS(["calendar_date", "is_trading_day"], rows)


def _members():
    return RS(["updateDate", "code", "code_name"], [["2026-10-26", "sh.600000", "浦发银行"]])


query_hs300_stocks = query_zz500_stocks = query_sz50_stocks = _members


def price(code, d):
    return round(5 + (zlib.crc32(code.encode()) % 50) + (d.toordinal() % 97) / 10, 2)


def query_history_k_data_plus(code, fields, start_date, end_date, frequency="d", adjustflag="3"):
    CALLS.append((code, frequency, adjustflag, start_date, end_date))
    fl = fields.split(",")
    ipo, out = next(((r[2], r[3]) for r in BASIC if r[0] == code), ("1990-01-01", ""))
    if code.startswith("sh.000") and frequency != "d":
        return RS(fl, [], "10004011", "指数不支持分钟线")
    if "peTTM" in fl and not code.startswith(("sh.6", "sz.0", "sz.3")):
        return RS(fl, [], "10004020", "参数错误：字段不支持")
    if "turn" in fl and code.startswith("sh.000"):
        return RS(fl, [], "10004020", "参数错误：字段不支持")
    if end_date >= date.today().isoformat():   # 「到今天為止」視為到假資料的 TODAY
        end_date = TODAY.isoformat()
    rows = []
    for d in trading_days(max(start_date, ipo), min(end_date, out or "9999")):
        p = price(code, d)
        mult = 1.5 if adjustflag == "1" else 1.0
        if frequency == "d":
            v = {"date": d.isoformat(), "code": code, "open": p, "high": p + 0.5,
                 "low": p - 0.5, "close": round(p * mult, 4), "preclose": p - 0.1,
                 "volume": 100000, "amount": 1e6, "turn": 1.2, "tradestatus": 1,
                 "pctChg": 0.5, "peTTM": 10, "pbMRQ": 1, "psTTM": 2,
                 "pcfNcfTTM": 3, "isST": 0}
            rows.append([str(v[f]) for f in fl])
        else:
            times = ["0945", "1000", "1015", "1030", "1045", "1100", "1115", "1130",
                     "1315", "1330", "1345", "1400", "1415", "1430", "1445", "1500"]
            for k, t in enumerate(times):
                v = {"date": d.isoformat(), "time": d.strftime("%Y%m%d") + t + "00000",
                     "code": code, "open": p + k, "high": p + k + 2, "low": p + k - 1,
                     "close": p + k + 1, "volume": 100 * (k + 1), "amount": 1000 * (k + 1)}
                rows.append([str(v[f]) for f in fl])
    return RS(fl, rows)
