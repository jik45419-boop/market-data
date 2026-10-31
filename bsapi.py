# -*- coding: utf-8 -*-
"""BaoStock 包裝：逾時保護、自動重新登入、欄位降級"""
import signal
import time

import baostock as bs

from common import ETF, INDEX, STOCK


class QueryTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise QueryTimeout("BaoStock 查詢逾時")


signal.signal(signal.SIGALRM, _alarm)


def login():
    last = None
    for i in range(5):
        try:
            signal.alarm(60)
            lg = bs.login()
            signal.alarm(0)
            if lg.error_code == "0":
                return
            last = f"{lg.error_code} {lg.error_msg}"
        except Exception as e:
            signal.alarm(0)
            last = repr(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"BaoStock 登入失敗: {last}")


def logout():
    try:
        signal.alarm(20)
        bs.logout()
    except Exception:
        pass
    finally:
        signal.alarm(0)


def relogin():
    logout()
    time.sleep(1)
    login()


def query(fn, *args, timeout=150, retries=3, **kw):
    """回傳 (fields, rows)；資料源回報錯誤時重試，最後拋出例外。"""
    err = None
    for attempt in range(1, retries + 1):
        try:
            signal.alarm(timeout)
            rs = fn(*args, **kw)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            signal.alarm(0)
            if rs.error_code == "0":
                return rs.fields, rows
            err = f"{rs.error_code} {rs.error_msg}"
            # 欄位不支援等參數錯誤，重試也沒用
            if "参数" in rs.error_msg or "字段" in rs.error_msg:
                break
        except Exception as e:
            signal.alarm(0)
            err = repr(e)
        if attempt < retries:
            time.sleep(2 * attempt)
            try:
                relogin()
            except Exception as e:
                err = f"{err}; relogin {e!r}"
    raise RuntimeError(err)


def kdata(code, fields, start, end, freq="d", adjust="3"):
    return query(bs.query_history_k_data_plus, code, fields,
                 start_date=start, end_date=end,
                 frequency=freq, adjustflag=adjust)


DAILY_FIELDS = {
    STOCK: ["date,code,open,high,low,close,preclose,volume,amount,turn,"
            "tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"],
    ETF: ["date,code,open,high,low,close,preclose,volume,amount,turn,"
          "tradestatus,pctChg",
          "date,code,open,high,low,close,preclose,volume,amount,pctChg",
          "date,code,open,high,low,close,volume,amount"],
    INDEX: ["date,code,open,high,low,close,preclose,volume,amount,pctChg",
            "date,code,open,high,low,close,volume,amount"],
}
_working_fields = {}

NAME_MAP = {"pctChg": "pct_chg", "peTTM": "pe_ttm", "pbMRQ": "pb_mrq",
            "psTTM": "ps_ttm", "pcfNcfTTM": "pcf_ttm", "isST": "is_st"}
INT_COLS = {"tradestatus", "is_st"}


def num(v):
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "-", "--", "None", "nan"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def daily(code, kind, start, end):
    """回傳 DAILY_COLS 順序的列；含後復權因子 adj_factor = 後復權收盤 / 不復權收盤"""
    candidates = DAILY_FIELDS[kind]
    if kind in _working_fields:
        candidates = [_working_fields[kind]] + [c for c in candidates if c != _working_fields[kind]]
    fields = rows = None
    err = None
    for f in candidates:
        try:
            fields, rows = kdata(code, f, start, end, "d", "3")
            _working_fields[kind] = f
            break
        except RuntimeError as e:
            err = e
    if fields is None:
        raise RuntimeError(f"日線查詢失敗 {code}: {err}")
    if not rows:
        return []

    factor = {}
    if kind != INDEX:
        _, hrows = kdata(code, "date,close", start, end, "d", "1")
        factor = {r[0]: num(r[1]) for r in hrows}

    idx = {NAME_MAP.get(n, n): i for i, n in enumerate(fields)}
    out = []
    for r in rows:
        rec = {}
        for col in ("open", "high", "low", "close", "preclose", "volume",
                    "amount", "turn", "tradestatus", "pct_chg", "pe_ttm",
                    "pb_mrq", "ps_ttm", "pcf_ttm", "is_st"):
            i = idx.get(col)
            v = num(r[i]) if i is not None else None
            if v is not None and col in INT_COLS:
                v = int(v)
            rec[col] = v
        d = r[idx["date"]]
        c = rec["close"]
        if kind == INDEX:
            adj = 1.0
        else:
            h = factor.get(d)
            adj = round(h / c, 8) if h and c else None
        out.append((code, d, rec["open"], rec["high"], rec["low"], c,
                    rec["preclose"], rec["volume"], rec["amount"], rec["turn"],
                    rec["tradestatus"], rec["pct_chg"], rec["pe_ttm"],
                    rec["pb_mrq"], rec["ps_ttm"], rec["pcf_ttm"], rec["is_st"],
                    adj))
    return out


def m15(code, start, end):
    """15 分鐘線（不復權）；dt 為 YYYYMMDDHHMM 整數"""
    _, rows = kdata(code, "date,time,code,open,high,low,close,volume,amount",
                    start, end, "15", "3")
    out = []
    for r in rows:
        t = r[1]
        if not t or len(t) < 12:
            continue
        out.append((code, int(t[:12]), num(r[3]), num(r[4]), num(r[5]),
                    num(r[6]), num(r[7]), num(r[8])))
    return out


def last_date(code, freq):
    """探測資料源最新可用日期"""
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=20)
    if freq == "d":
        _, rows = kdata(code, "date,close", start.isoformat(), end.isoformat(), "d", "3")
        return rows[-1][0] if rows else None
    _, rows = kdata(code, "date,time,close", (end - timedelta(days=10)).isoformat(),
                    end.isoformat(), freq, "3")
    return rows[-1][0] if rows else None
