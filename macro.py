# -*- coding: utf-8 -*-
"""
股市相關數據（歷年）：宏觀、貨幣、商品、匯率、國家隊／社保、資金面、估值情緒、衍生品、海外
  一份資料庫 data/macro.db（Drive 上是 macro.db.gz），每一種數據一張表，原始欄位照存，另外加一欄「期間」
  （統一成 YYYY-MM-DD／YYYY-MM／YYYYQn，由舊到新排序，方便畫圖和對齊）。
  表 _datasets：每張表的中文名稱、分類、來源、筆數、起訖、最後成功時間、最後錯誤、備註
  表 _progress：逐日／逐季／逐年抓的數據，記錄哪些期間已經抓完（中斷了下次接著抓）

用法：
  python macro.py                      抓全部（每次最多 BUDGET_MIN 分鐘，沒抓完的下次繼續）
  python macro.py --only cpi,ppi       只抓指定的數據
  python macro.py --list               列出全部數據
  python macro.py --export out/        匯出 Excel（每種數據一頁）＋目錄
任何一種數據失敗只記在 _datasets.last_error，不影響其他數據。
"""
import argparse
import io
import os
import re
import sqlite3
import sys
import time
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")
warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import requests  # noqa: E402

TZ = timezone(timedelta(hours=8))
DATA = Path(os.environ.get("DATA_DIR", "data"))
DB = DATA / "macro.db"
BUDGET_MIN = float(os.environ.get("BUDGET_MIN", "300"))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_deadline = None


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def today():
    return datetime.now(TZ).date()


def ak():
    import akshare
    return akshare


# ======================================================================== 期間欄位統一
_CN_Q = {"1": "Q1", "1-2": "Q2", "1-3": "Q3", "1-4": "Q4", "2": "Q2", "3": "Q3", "4": "Q4"}


def norm_period(v):
    """各種日期寫法 → YYYY-MM-DD／YYYY-MM／YYYYQn；認不出來回傳 None"""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s) or re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", s) \
        or re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s) or re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})年?第([\d\-]+)季度", s)          # 「2026年第1-3季度」「2008第1季度」都認得
    if m:
        return f"{m.group(1)}{_CN_Q.get(m.group(2), 'Q' + m.group(2)[-1])}"
    m = re.match(r"^(\d{4})年(\d{1,2})月", s) or re.match(r"^(\d{4})[.\-](\d{1,2})$", s) or re.match(r"^(\d{4})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def add_period(df, col=None):
    """在最前面加「期間」欄並由舊到新排序；col＝日期欄（不給就自動找第一個認得出日期的欄）"""
    if df is None or df.empty or "期間" in df.columns:
        return df.sort_values("期間", kind="stable").reset_index(drop=True) if df is not None and "期間" in df.columns else df
    if col:
        cols = [col]
    else:       # 名稱像日期的欄位優先（避免把股票代碼當成日期）
        key = re.compile(r"日|期|时间|時間|月|季|date|dt|time", re.I)
        cols = [c for c in df.columns if key.search(str(c))] + [c for c in df.columns if not key.search(str(c))]
    for c in cols:
        p = df[c].map(norm_period)
        ok = p.dropna()
        if len(ok) and p.notna().mean() > 0.8 and ok.str[:4].astype(int).between(1900, 2100).all():
            df = df.copy()
            df.insert(0, "期間", p)
            return df.sort_values("期間", kind="stable").reset_index(drop=True)
    return df


# ======================================================================== 抓取小工具
def http_get(url, params=None, headers=None, timeout=30, tries=3):
    err = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (i + 1))
    raise err


def em_dc(report, flt="", cols="ALL", sort="", page_size=500, max_pages=400):
    """東方財富 數據中心：抓全部頁"""
    rows, page = [], 1
    while page <= max_pages:
        p = {"reportName": report, "columns": cols, "pageNumber": page, "pageSize": page_size,
             "source": "WEB", "client": "WEB"}
        if sort:
            p.update(sortColumns=sort, sortTypes="-1")
        if flt:
            p["filter"] = flt
        js = http_get(DC, p, {"Referer": "https://data.eastmoney.com/"}).json()
        res = js.get("result") or {}
        rows += res.get("data") or []
        if page >= (res.get("pages") or 1):
            break
        page += 1
        time.sleep(0.3)
    return pd.DataFrame(rows)


def em_dc_by_year(report, flt, date_col, cols="ALL", start=2000):
    """東方財富 數據中心最多只給 100 頁（5 萬筆）：按年份切開抓"""
    out = []
    for y in range(start, today().year + 1):
        f = f"{flt}({date_col}>='{y}-01-01')({date_col}<='{y}-12-31')"
        d = em_dc(report, f, cols=cols, sort=date_col)
        if len(d):
            out.append(d)
        time.sleep(0.3)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def fred(ids):
    """FRED（聖路易聯儲）：多個序列合成一張長表 series, date, value"""
    out, bad = [], []
    for sid in ids:
        try:
            r = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": sid}, timeout=30, tries=2)
        except Exception as e:  # noqa: BLE001
            bad.append(sid)
            log(f"  FRED {sid} 失敗：{type(e).__name__}")
            if len(bad) >= 2 and not out:      # GitHub 主機常連不到 FRED：前 2 個失敗就放棄，下次再試
                raise
            continue
        d = pd.read_csv(io.StringIO(r.text))
        d.columns = ["date", "value"]
        d["value"] = pd.to_numeric(d["value"], errors="coerce")
        d = d.dropna()
        d.insert(0, "series", sid)
        out.append(d)
        time.sleep(0.5)
    if not out:
        raise RuntimeError("FRED 全部失敗")
    return pd.concat(out, ignore_index=True)


def em_kline(secid, name):
    """東方財富 日 K（美元指數等）"""
    q = (f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=101&fqt=0&beg=19900101&end=20500101&lmt=100000")
    err = None
    for h in ("push2his.eastmoney.com", "63.push2his.eastmoney.com", "7.push2his.eastmoney.com"):
        try:
            js = http_get(f"https://{h}/api/qt/stock/kline/get?{q}", headers={"Referer": "https://quote.eastmoney.com/"}, tries=2).json()
            ks = ((js.get("data") or {}).get("klines")) or []
            return pd.DataFrame([k.split(",")[:5] for k in ks], columns=["date", "open", "close", "high", "low"])
        except Exception as e:  # noqa: BLE001
            err = e
    raise err


def concat_named(items):
    """[(名稱, 函式)] → 一張表，多一欄「品種」；個別品種失敗會重試，仍失敗就略過（全部失敗才算失敗）"""
    out, bad = [], []
    for name, fn in items:
        d = None
        for i in range(3):
            try:
                d = fn()
                break
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(5 * (i + 1))
        if d is None or len(d) == 0:
            bad.append(name)
            log(f"  {name} 失敗：{type(err).__name__ if d is None else '沒有資料'}")
            if len(bad) >= 3 and not out:      # 前 3 個都失敗：來源大概整個連不上，不要再浪費時間
                raise RuntimeError(f"前 {len(bad)} 個品種都失敗，來源可能連不上：{type(err).__name__ if d is None else '沒有資料'}")
            continue
        d = d.copy()
        d.insert(0, "品種", name)
        out.append(d)
        time.sleep(0.5)
    if not out:
        raise RuntimeError("全部品種都失敗")
    return pd.concat(out, ignore_index=True)


# ======================================================================== 逐期抓的期間清單
def quarter_ends_cur(start):
    """已結束的每個季末日，再加上「當季」的季末日（還沒到的也算，讓當季資料每次重抓）"""
    out = quarter_ends(start)
    t = today()
    m = ((t.month - 1) // 3 + 1) * 3
    cur = f"{t.year}-{m:02d}-{'30' if m in (6, 9) else '31'}"
    if cur not in out:
        out.append(cur)
    return out


def quarter_ends(start):
    y, out = int(start[:4]), []
    t = today()
    while True:
        for md in ("03-31", "06-30", "09-30", "12-31"):
            d = f"{y}-{md}"
            if d < start:
                continue
            if d > t.isoformat():
                return out
            out.append(d)
        y += 1


_cal = None


def trade_days(start):
    global _cal
    if _cal is None:
        d = ak().calendar_trade_date_hist_sina()
        _cal = sorted(str(x)[:10] for x in d["trade_date"])
    t = today().isoformat()
    return [d for d in _cal if start <= d <= t]


def sw_months(start):
    """申萬月報的日期（每月最後一個交易日）"""
    d = ak().index_analysis_week_month_sw(symbol="month")
    return sorted(str(x)[:10] for x in d.iloc[:, 0] if str(x)[:10] >= start)


def month_starts(start):
    """每月第一個交易日（查指數成分用）"""
    out, seen = [], set()
    for d in trade_days(start):
        if d[:7] not in seen:
            seen.add(d[:7])
            out.append(d)
    return out


def years(start):
    return [str(y) for y in range(int(start), today().year + 1)]


# ======================================================================== 數據清單
# 一般數據：(id, 中文名稱, 分類, 來源說明, 抓取函式, 備註)
#   每次整張重抓（官方數據會修正），抓失敗就保留舊的
# 逐期數據：(id, 中文名稱, 分類, 來源說明, ("quarter"|"day"|"year", 起點, 每期抓取函式, 每次重抓最近幾期), 備註)
A = ak  # 延遲載入

INDEX_PE = ("上证50", "沪深300", "中证500", "中证1000", "创业板50", "中证800", "深证红利", "上证红利")
QVIX = (("50ETF", "index_option_50etf_qvix"), ("300ETF", "index_option_300etf_qvix"),
        ("500ETF", "index_option_500etf_qvix"), ("创业板ETF", "index_option_cyb_qvix"),
        ("科创50ETF", "index_option_kcb_qvix"), ("沪深300指数", "index_option_300index_qvix"),
        ("中证1000指数", "index_option_1000index_qvix"), ("上证50指数", "index_option_50index_qvix"))
CN_FUT = (("黄金 AU", "AU0"), ("白银 AG", "AG0"), ("原油 SC", "SC0"), ("铜 CU", "CU0"), ("铝 AL", "AL0"),
          ("螺纹钢 RB", "RB0"), ("铁矿石 I", "I0"), ("焦煤 JM", "JM0"), ("焦炭 J", "J0"), ("豆粕 M", "M0"),
          ("生猪 LH", "LH0"), ("玉米 C", "C0"), ("PTA TA", "TA0"), ("沪深300股指 IF", "IF0"),
          ("上证50股指 IH", "IH0"), ("中证500股指 IC", "IC0"), ("中证1000股指 IM", "IM0"), ("10年国债 T", "T0"))
FOREIGN_FUT = (("COMEX 黄金", "GC"), ("COMEX 白银", "SI"), ("COMEX 铜", "HG"), ("NYMEX WTI 原油", "CL"),
               ("布伦特原油", "OIL"), ("NYMEX 天然气", "NG"), ("富时中国 A50", "CHA50CFD"), ("LME 铜", "CAD"),
               ("CBOT 大豆", "S"))          # 日經 225（NKD）來源 2019-05 後停更，已拿掉
GLOBAL_IDX = (("标普500", lambda: A().index_us_daily_sina(symbol=".INX")),
              ("纳斯达克", lambda: A().index_us_daily_sina(symbol=".IXIC")),
              ("道琼斯", lambda: A().index_us_daily_sina(symbol=".DJI")),
              ("纳斯达克中国金龙", lambda: A().index_us_daily_sina(symbol=".HXC")),
              ("恒生指数", lambda: A().index_hk_daily_sina(symbol="HSI")),
              ("恒生国企", lambda: A().index_hk_daily_sina(symbol="HSCEI")),
              ("恒生科技", lambda: A().index_hk_daily_sina(symbol="HSTECH")))
CN_IDX = (("上证指数", "sh000001"), ("深证成指", "sz399001"), ("沪深300", "sh000300"), ("上证50", "sh000016"),
          ("中证500", "sh000905"), ("中证1000", "sh000852"), ("创业板指", "sz399006"), ("科创50", "sh000688"),
          ("中证红利", "sh000922"), ("中证全指", "sh000985"))
FRED_IDS = {
    "PCEPI": "美国 PCE 物价指数", "PCEPILFE": "美国核心 PCE 物价指数", "ICSA": "美国初请失业金人数（周）",
    "UMCSENT": "密歇根消费者信心", "DFF": "联邦基金有效利率", "DGS2": "美国 2 年国债收益率", "DGS10": "美国 10 年国债收益率",
    "T10Y2Y": "美债 10 年减 2 年利差", "DFII10": "美国 10 年实际利率（TIPS）", "BAMLH0A0HYM2": "美国高收益债利差",
    "WALCL": "美联储资产负债表总资产（周）", "WTREGEN": "美国财政部 TGA 账户余额（周）", "RRPONTSYD": "美联储隔夜逆回购余额",
    "M2SL": "美国 M2", "VIXCLS": "VIX 恐慌指数", "DTWEXBGS": "美元指数（广义贸易加权）", "DEXCHUS": "美元兑人民币",
    "DEXJPUS": "美元兑日元", "DCOILWTICO": "WTI 原油现货", "DCOILBRENTEU": "布伦特原油现货", "CBBTCUSD": "比特币价格",
    "IRLTLT01JPM156N": "日本 10 年国债收益率（月）", "NFCI": "芝加哥联储金融状况指数", "SP500": "标普 500",
    "NASDAQCOM": "纳斯达克综合指数", "INDPRO": "美国工业生产指数", "PAYEMS": "美国非农就业总数", "UNRATE": "美国失业率",
    "CPIAUCSL": "美国 CPI 指数", "CPILFESL": "美国核心 CPI 指数", "GDPC1": "美国实际 GDP（季）", "MORTGAGE30US": "美国 30 年房贷利率",
}
NT_EXTRA = ("梧桐树投资平台有限责任公司", "北京诚通金控投资有限公司", "国新投资有限公司", "中国国有企业结构调整基金股份有限公司",
            "中国国有企业结构调整基金二期股份有限公司", "国新发展投资管理有限公司", "中国国新控股有限责任公司", "中国诚通控股集团有限公司")
HOLDER_COLS = {"END_DATE": "报告期", "NOTICE_DATE": "公告日", "SECURITY_CODE": "股票代码", "SECURITY_NAME_ABBR": "股票简称",
               "HOLDER_NAME": "股东名称", "HOLDER_NEWTYPE": "股东类型", "HOLDER_RANK": "名次", "HOLD_NUM": "持股数",
               "FREE_HOLDNUM_RATIO": "占流通股比例%", "HOLD_NUM_CHANGE": "增减", "CHANGE_RATIO": "变动比例%",
               "HOLDER_MARKET_CAP": "持股市值", "SHARES_TYPE": "股份类型"}


def holders(flt):
    d = em_dc_by_year("RPT_F10_EH_FREEHOLDERS", flt, "END_DATE", cols=",".join(HOLDER_COLS), start=2004)
    # 來源同一筆（同股票、同報告期、同股東、同持股數）常重複 3～5 次，只留一筆
    d = d.drop_duplicates(["SECURITY_CODE", "END_DATE", "HOLDER_NAME", "HOLD_NUM"])
    return d.rename(columns=HOLDER_COLS)


def epu():
    """中國經濟政策不確定性指數（Baker-Bloom-Davis；Davis-Liu-Sheng）：多張表合成長表"""
    out = []
    for url, tag in (("https://www.policyuncertainty.com/media/China_Mainland_Paper_EPU.xlsx", "大陆报纸"),
                     ("https://www.policyuncertainty.com/media/SCMP_China_Policy_Uncertainty_Data.xlsx", "南华早报")):
        r = http_get(url, timeout=60)
        for sheet, d in pd.read_excel(io.BytesIO(r.content), sheet_name=None).items():
            d = d.iloc[:, :3].dropna()
            d.columns = ["year", "month", "value"]
            d = d[pd.to_numeric(d["year"], errors="coerce").notna()]
            d["期間"] = [f"{int(y)}-{int(m):02d}" for y, m in zip(d["year"], d["month"])]
            d.insert(0, "指数", f"{tag}：{sheet}")
            out.append(d[["指数", "期間", "value"]].rename(columns={"value": "数值"}))
    return pd.concat(out, ignore_index=True)


def industry_indicators():
    """東方財富 行業指標（運價、鋼鐵、有色、汽車、手機、貨運、電力、地產銷售、保費…，約 150 項）"""
    cols = "REPORT_DATE,BOARD_NAME,INDICATOR_ID,INDICATOR_NAME,INDICATOR_VALUE,CHANGE_RATE"
    d = em_dc_by_year("RPT_INDUSTRY_INDEX", "", "REPORT_DATE", cols=cols, start=1990)
    d = d.drop_duplicates(["INDICATOR_ID", "REPORT_DATE"])
    return d.rename(columns={"REPORT_DATE": "日期", "BOARD_NAME": "所属行业", "INDICATOR_ID": "指标代码",
                             "INDICATOR_NAME": "指标", "INDICATOR_VALUE": "数值", "CHANGE_RATE": "变动%"})


def sw_industry_daily():
    info = A().index_first_info_sw()
    items = [(n, (lambda c=c.split(".")[0]: A().index_hist_sw(symbol=c, period="day")))
             for c, n in zip(info["行业代码"], info["行业名称"])]
    return concat_named(items)


SZ_HDR = {"Referer": "https://fund.szse.cn/marketdata/fundslist/index.html"}
SZ_FALLBACK = ("159919", "159915", "159922", "159901", "159949", "159845", "159952", "159925")
_sz_top = None


def szse_top_etfs(n=40):
    """深交所規模最大的 n 檔股票型 ETF（抓不到清單就用固定的寬基 ETF）"""
    global _sz_top
    if _sz_top is None:
        try:
            d = A().fund_etf_scale_szse()
            d = d[d["投资类别"].astype(str).str.contains("股票")].sort_values("基金份额", ascending=False)
            _sz_top = list(dict.fromkeys(list(SZ_FALLBACK) + list(d["基金代码"].astype(str).head(n))))
        except Exception as e:  # noqa: BLE001
            log(f"  深交所 ETF 清單抓不到（{type(e).__name__}），用固定的 {len(SZ_FALLBACK)} 檔寬基 ETF")
            _sz_top = list(SZ_FALLBACK)
    return _sz_top


def szse_size(code, start, end):
    """深交所單一基金在 start～end 的每日份額（一次最多約 4 個月，每頁 20 筆）"""
    rows, page = [], 1
    while True:
        p = {"SHOWTYPE": "JSON", "CATALOGID": "fund_jjgm", "TABKEY": "tab1", "txtDm": code,
             "txtStart": start, "txtEnd": end, "PAGENO": page}
        js = http_get("https://fund.szse.cn/api/report/ShowReport/data", p, SZ_HDR, timeout=20, tries=6).json()
        t = js[0]
        rows += t.get("data") or []
        if page >= (t.get("metadata") or {}).get("pagecount", 0):
            break
        page += 1
        time.sleep(0.4)
    return rows


def quarter_window(qend, now=None):
    """季末日 → (該季第一天, 查詢截止日)；當季還沒結束時截止日是今天"""
    y, mo = int(qend[:4]), int(qend[5:7])
    start = f"{y}-{((mo - 1) // 3) * 3 + 1:02d}-01"
    return start, min(qend, (now or today()).isoformat())


def szse_etf_quarter(qend):
    """一季（含還沒結束的當季）：前 40 大股票型 ETF 的每日份額（份）"""
    qstart, qstop = quarter_window(qend)
    out = []
    for code in szse_top_etfs():
        for r in szse_size(code, qstart, qstop):
            out.append({"日期": r["size_date"], "基金代码": r["fund_code"], "基金简称": r["security_short_name"],
                        "基金份额": float(str(r["current_size"]).replace(",", "")) * 1e4})
        time.sleep(0.3)
    return pd.DataFrame(out)


CB_AREAS = {"US": "美国 联准会", "XM": "欧元区 欧洲央行", "GB": "英国 英格兰银行", "JP": "日本 日本央行", "CN": "中国 人民银行",
            "TW": "台湾 中央银行", "KR": "韩国 韩国银行", "HK": "香港 金管局", "CA": "加拿大", "AU": "澳洲", "CH": "瑞士",
            "IN": "印度", "BR": "巴西", "MX": "墨西哥", "RU": "俄罗斯", "TR": "土耳其", "SE": "瑞典", "NZ": "纽西兰"}
_cb = None
TW_CBC_URL = "https://www.cbc.gov.tw/public/data/OpenData/%E7%B6%93%E7%A0%94%E8%99%95/EG28D01.csv"


def bis_policy_daily():
    """國際清算銀行 各國央行政策利率（每日）"""
    global _cb
    if _cb is None:
        url = ("https://stats.bis.org/api/v1/data/WS_CBPOL/D." + "+".join(CB_AREAS)
               + "/all?format=csv&startPeriod=1990-01-01")
        d = pd.read_csv(io.StringIO(http_get(url, timeout=120).text), usecols=["REF_AREA", "TIME_PERIOD", "OBS_VALUE"])
        d["OBS_VALUE"] = pd.to_numeric(d["OBS_VALUE"], errors="coerce")
        d = d.dropna()
        try:      # 台灣：BIS 沒有，改用中央銀行開放資料（重貼現率，只有調整日）
            tw = pd.read_csv(io.StringIO(http_get(TW_CBC_URL, timeout=60).content.decode("utf-8-sig")), dtype=str)
            tw = pd.DataFrame({"REF_AREA": "TW", "TIME_PERIOD": pd.to_datetime(tw.iloc[:, 0]).dt.strftime("%Y-%m-%d"),
                               "OBS_VALUE": pd.to_numeric(tw.iloc[:, 1], errors="coerce")}).dropna()
            d = pd.concat([d[d["REF_AREA"] != "TW"], tw], ignore_index=True)
        except Exception as e:  # noqa: BLE001
            log(f"  台灣央行利率抓不到：{type(e).__name__}")
        _cb = d.sort_values(["REF_AREA", "TIME_PERIOD"]).reset_index(drop=True)
    return _cb


def _runs(g, min_days=7):
    """一個國家的利率序列 → 去掉維持不到 min_days 天的短暫跳動（早期用市場利率的國家）後的 [(日期, 利率)]"""
    vals, dates = list(g["OBS_VALUE"]), list(g["TIME_PERIOD"])
    runs = []
    for d, v in zip(dates, vals):
        if not runs or abs(runs[-1][1] - v) > 1e-9:
            runs.append([d, v])
    keep = []
    for i, (d, v) in enumerate(runs):
        nxt = runs[i + 1][0] if i + 1 < len(runs) else None
        if nxt is None or (date.fromisoformat(nxt) - date.fromisoformat(d)).days >= min_days:
            if not keep or abs(keep[-1][1] - v) > 1e-9:
                keep.append((d, v))
    return keep


def cb_rates_monthly():
    d = bis_policy_daily().copy()
    d["月"] = d["TIME_PERIOD"].str[:7]
    w = d.groupby(["月", "REF_AREA"])["OBS_VALUE"].last().unstack()
    months = pd.period_range("1990-01", today().strftime("%Y-%m"), freq="M").strftime("%Y-%m")
    w = w.reindex(months).ffill()            # 台灣只有調整日：沿用上一次的利率
    w = w[[c for c in CB_AREAS if c in w.columns]]
    return w.rename(columns=CB_AREAS).rename_axis("期間").reset_index()


def cb_rate_changes():
    """每一次升息／降息：日期、國家、調整前後、幅度（基點），加上之後 20 個交易日的上證、標普 500 漲跌"""
    d = bis_policy_daily()
    out = []
    for area, g in d.groupby("REF_AREA"):
        runs = _runs(g)
        for (d0, v0), (d1, v1) in zip(runs, runs[1:]):
            bp = round((v1 - v0) * 100, 1)
            if abs(bp) < 5:            # 小於 5 基點的變動多半是市場利率波動，不是政策調整
                continue
            out.append({"日期": d1, "央行": CB_AREAS.get(area, area), "代码": area, "调整前%": v0,
                        "调整后%": v1, "幅度(基点)": bp, "方向": "升息" if bp > 0 else "降息"})
    ch = pd.DataFrame(out).sort_values(["日期", "代码"]).reset_index(drop=True)
    try:                      # 市場反應：用前面抓到的指數日線
        con = sqlite3.connect(str(DB))
        for name, sql in (("上证", "select 期間 d, close c from cn_index_daily where 品種='上证指数' order by 期間"),
                          ("标普500", "select 期間 d, close c from global_index where 品種='标普500' order by 期間")):
            px = pd.read_sql(sql, con)
            days, closes = list(px["d"]), [float(x) for x in px["c"]]
            import bisect
            r = []
            for dt in ch["日期"]:
                i = bisect.bisect_left(days, dt)
                j = i + 19
                r.append(round((closes[j] / closes[i - 1] - 1) * 100, 2) if 0 < i and j < len(days) else None)
            ch[f"{name}之后20日%"] = r
        con.close()
    except Exception:  # noqa: BLE001
        pass
    return ch


NYFED = "https://markets.newyorkfed.org/api"


def us_initial_claims():
    """美國勞工部 各州每週失業金申請（ar539）→ 全國合計：初領、續領（未季調）"""
    d = pd.read_csv(io.StringIO(http_get("https://oui.doleta.gov/unemploy/csv/ar539.csv", timeout=120).text),
                    usecols=["st", "rptdate", "c3", "c8"])
    d["c3"] = pd.to_numeric(d["c3"], errors="coerce")
    d["c8"] = pd.to_numeric(d["c8"], errors="coerce")
    g = d.groupby("rptdate").agg(初领失业金人数=("c3", "sum"), 续领失业金人数=("c8", "sum"), 州数=("st", "nunique"))
    return g.reset_index().rename(columns={"rptdate": "日期"})


def us_rrp():
    """紐約聯儲 隔夜逆回購（ON RRP）每日接受金額"""
    js = http_get(f"{NYFED}/rp/reverserepo/propositions/search.json", {"startDate": "2013-09-01"}, timeout=120).json()
    rows = [{"日期": o["operationDate"], "接受金额(亿美元)": float(o.get("totalAmtAccepted") or 0) / 1e8}
            for o in js["repo"]["operations"]]
    return pd.DataFrame(rows).groupby("日期", as_index=False)["接受金额(亿美元)"].sum()


def us_soma():
    """紐約聯儲 SOMA 持有證券（聯準會資產負債表的主體，每週）"""
    js = http_get(f"{NYFED}/soma/summary.json", timeout=120).json()
    d = pd.DataFrame(js["soma"]["summary"])
    for c in d.columns:
        if c != "asOfDate":
            d[c] = pd.to_numeric(d[c], errors="coerce") / 1e8
    return d.rename(columns={"asOfDate": "日期", "total": "合计(亿美元)", "bills": "国库券", "notesbonds": "中长期国债",
                             "tips": "抗通胀债TIPS", "mbs": "房贷抵押债MBS", "cmbs": "商业房贷债CMBS", "agencies": "机构债",
                             "frn": "浮动利率债", "tipsInflationCompensation": "TIPS通胀补偿"})


def us_nyfed_rates():
    """紐約聯儲 有效聯邦基金利率 EFFR（2000 起）、SOFR（2018 起）"""
    out = []
    for path, start in (("unsecured/effr", "2000-07-01"), ("secured/sofr", "2018-04-01")):
        js = http_get(f"{NYFED}/rates/{path}/search.json", {"startDate": start}, timeout=120).json()
        for r in js["refRates"]:
            out.append({"日期": r["effectiveDate"], "利率": r["type"], "数值%": r.get("percentRate"),
                        "目标下限%": r.get("targetRateFrom"), "目标上限%": r.get("targetRateTo"),
                        "成交量(十亿美元)": r.get("volumeInBillions")})
    return pd.DataFrame(out)


def us_tga():
    """美國財政部 每日報表（DTS）TGA 帳戶餘額（百萬美元）"""
    url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
    types = ("Treasury General Account (TGA) Closing Balance", "Treasury General Account (TGA) Opening Balance",
             "Treasury General Account (TGA)", "Federal Reserve Account")      # 帳戶名稱歷年改過三次
    rows, page = [], 1
    while True:
        p = {"fields": "record_date,account_type,open_today_bal,close_today_bal", "sort": "record_date",
             "page[size]": "10000", "page[number]": str(page)}       # 帳戶名稱有括號，伺服器端篩選會漏，改成自己篩
        js = http_get(url, p, {"Accept": "application/json"}, timeout=120).json()
        rows += js.get("data") or []
        if page >= (js.get("meta") or {}).get("total-pages", 1):
            break
        page += 1
    d = pd.DataFrame(rows)
    d = d[d["account_type"].isin(types)]
    for c in ("open_today_bal", "close_today_bal"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    out = {}
    for r in d.itertuples():          # 收盤餘額優先；2021-10 改版後只有「隔天的開盤餘額」，用開盤餘額
        v = r.close_today_bal if r.close_today_bal == r.close_today_bal else r.open_today_bal
        if v == v and (r.record_date not in out or "Opening" not in r.account_type):
            out[r.record_date] = v
    return pd.DataFrame({"日期": list(out), "TGA余额(百万美元)": list(out.values())})


def cboe_vix():
    d = pd.read_csv(io.StringIO(http_get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                                         timeout=60).text))
    d["DATE"] = pd.to_datetime(d["DATE"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d")
    return d.rename(columns={"DATE": "日期"})


GLOBAL_MACRO = (
    ("德国 CPI 年率", "macro_germany_cpi_yoy"), ("德国 GDP", "macro_germany_gdp"), ("德国 IFO 商业景气", "macro_germany_ifo"),
    ("德国 ZEW 经济景气", "macro_germany_zew"), ("德国 零售销售月率", "macro_germany_retail_sales_mom"),
    ("德国 贸易帐", "macro_germany_trade_adjusted"),
    ("英国 CPI 年率", "macro_uk_cpi_yoy"), ("英国 核心 CPI 年率", "macro_uk_core_cpi_yoy"), ("英国 GDP 季率", "macro_uk_gdp_qoq"),
    ("英国 失业率", "macro_uk_unemployment_rate"), ("英国 零售销售年率", "macro_uk_retail_sales_yoy"),
    ("英国 Halifax 房价年率", "macro_uk_halifax_yoy"), ("英国 贸易帐", "macro_uk_trade"),
    ("日本 核心 CPI 年率", "macro_japan_core_cpi_yoy"), ("日本 失业率", "macro_japan_unemployment_rate"),
    ("日本 领先指标", "macro_japan_leading_indicator"),
    ("加拿大 CPI 年率", "macro_canada_cpi_yoy"), ("加拿大 核心 CPI 年率", "macro_canada_core_cpi_yoy"),
    ("加拿大 GDP 月率", "macro_canada_gdp_mom"), ("加拿大 失业率", "macro_canada_unemployment_rate"),
    ("澳洲 CPI 年率", "macro_australia_cpi_yoy"), ("澳洲 失业率", "macro_australia_unemployment_rate"),
)


def global_macro():
    """歐、英、日、加、澳主要總經（金十，含公布日；「时间」是公布月份）"""
    d = concat_named([(n, getattr(A(), f)) for n, f in GLOBAL_MACRO])
    return d.rename(columns={"品種": "指标"})


CSI_INDUSTRY = (("能源", "000986"), ("原材料", "000987"), ("工业", "000988"), ("可选消费", "000989"), ("主要消费", "000990"),
                ("医药卫生", "000991"), ("金融地产", "000992"), ("信息技术", "000993"), ("电信业务", "000994"), ("公用事业", "000995"))


def csi_industry_daily():
    end = today().strftime("%Y%m%d")
    return concat_named([(n, (lambda c=c: csindex_hist(c))) for n, c in CSI_INDUSTRY])


_bs_on = False


def index_members_at(d):
    """BaoStock：某一天的滬深 300、中證 500、上證 50 成分股"""
    global _bs_on
    import baostock as bs
    import bsapi
    if not _bs_on:
        bsapi.login()
        _bs_on = True
    out = []
    for name, fn in (("沪深300", bs.query_hs300_stocks), ("中证500", bs.query_zz500_stocks), ("上证50", bs.query_sz50_stocks)):
        fields, rows = bsapi.query(fn, date=d)
        f = {x: i for i, x in enumerate(fields)}
        out += [{"指数": name, "代码": r[f["code"]], "名称": r[f["code_name"]], "成分更新日": r[f["updateDate"]]} for r in rows]
    if not out:
        raise RuntimeError("BaoStock 沒有回傳成分股")
    return pd.DataFrame(out)


def index_changes_from(snap):
    """成分股快照（_期, 指数, 代码, 名称, 成分更新日）→ 調入／調出紀錄"""
    out = []
    for name, g in snap.groupby("指数"):
        prev = None
        for d, h in g.groupby("_期", sort=True):
            cur = dict(zip(h["代码"], h["名称"]))
            eff = h["成分更新日"].max()
            if prev is not None:
                for c in sorted(set(cur) - set(prev)):
                    out.append({"生效日": eff, "指数": name, "变动": "调入", "代码": c, "名称": cur[c], "查到的月份": d[:7]})
                for c in sorted(set(prev) - set(cur)):
                    out.append({"生效日": eff, "指数": name, "变动": "调出", "代码": c, "名称": prev[c], "查到的月份": d[:7]})
            prev = cur
    return pd.DataFrame(out, columns=["生效日", "指数", "变动", "代码", "名称", "查到的月份"])


def index_changes():
    con = sqlite3.connect(str(DB))
    snap = pd.read_sql('select "_期", 指数, 代码, 名称, 成分更新日 from index_members_hist', con)
    con.close()
    return index_changes_from(snap)


KEY_ETFS = (("510050", "上证50 华夏"), ("510300", "沪深300 华泰柏瑞"), ("510310", "沪深300 易方达"), ("510330", "沪深300 华夏"),
            ("510500", "中证500 南方"), ("512100", "中证1000 南方"), ("588000", "科创50 华夏"), ("159919", "沪深300 嘉实"),
            ("159915", "创业板 易方达"), ("159922", "中证500 嘉实"), ("159901", "深证100 易方达"), ("159949", "创业板50 华安"),
            ("159845", "中证1000 华夏"), ("159952", "创业板 广发"))


def _etf_one(code):
    nav = A().fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    nav = pd.DataFrame({"日期": nav.iloc[:, 0].astype(str), "单位净值": pd.to_numeric(nav.iloc[:, 1], errors="coerce")})
    px = A().fund_etf_hist_sina(symbol=("sh" if code.startswith(("5", "6")) else "sz") + code)
    px = pd.DataFrame({"日期": px["date"].astype(str), "收盘": pd.to_numeric(px["close"], errors="coerce"),
                       "成交额": pd.to_numeric(px.get("amount"), errors="coerce")})
    d = px.merge(nav, on="日期", how="outer").sort_values("日期")
    d["溢价率%"] = ((d["收盘"] / d["单位净值"] - 1) * 100).round(3)
    # ETF 溢價很少超過 ±15%（2015 股災最深約 −14%）。差距超過 30%＝份額折算日（價格與淨值一邊已折算、一邊還沒）或來源錯誤
    bad = d["溢价率%"].abs() > 30
    d["备注"] = ""
    d.loc[bad, "备注"] = "价格与净值差距过大（份额折算日或来源错误），溢价率不采用"
    d.loc[bad, "溢价率%"] = None
    return d


def etf_premium():
    """主要寬基 ETF 每日收盤價、單位淨值、溢價率"""
    d = concat_named([(f"{c} {n}", (lambda c=c: _etf_one(c))) for c, n in KEY_ETFS])
    d.insert(1, "基金代码", d["品種"].str[:6])
    return d


def etf_flows():
    """主要寬基 ETF 每日估算淨申購（億元）＝份額變化 × 當天單位淨值；用上交所／深交所份額表和 etf_premium 的淨值"""
    con = sqlite3.connect(str(DB))
    have = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    sh = []
    if "etf_shares_sse" in have:
        sh.append(pd.read_sql('select substr(统计日期,1,10) 日期, 基金代码, 基金份额 from etf_shares_sse', con))
    if "etf_shares_szse" in have:
        sh.append(pd.read_sql('select 日期, 基金代码, 基金份额 from etf_shares_szse', con))
    nav = pd.read_sql('select 日期, 基金代码, 品種, 单位净值 from etf_premium', con) if "etf_premium" in have else None
    con.close()
    if not sh or nav is None:
        raise RuntimeError("還沒有 ETF 份額或淨值資料")
    sh = pd.concat(sh, ignore_index=True)
    sh["基金代码"] = sh["基金代码"].astype(str)
    sh = sh[sh["基金代码"].isin([c for c, _ in KEY_ETFS])].drop_duplicates(["基金代码", "日期"]).sort_values(["基金代码", "日期"])
    d = sh.merge(nav, on=["日期", "基金代码"], how="left")
    cal = {dd: i for i, dd in enumerate(trade_days("2004-01-01"))}
    d["_i"] = d["日期"].map(cal)
    d["份额变化"] = d.groupby("基金代码")["基金份额"].diff()
    gap = d.groupby("基金代码")["_i"].diff()
    d.loc[gap != 1, "份额变化"] = None          # 前一筆不是前一個交易日（中間有缺）：不算，避免把好幾天的變化算成一天
    # 份額折算／合併：份額比例 × 淨值比例 ≈ 1 且份額大幅變動（例如 2015-04-14 南方中證 500：份額 ÷3.57、淨值 ×3.57）→ 不是申購贖回
    sr = d["基金份额"] / d.groupby("基金代码")["基金份额"].shift(1)
    nr = d["单位净值"] / d.groupby("基金代码")["单位净值"].shift(1)
    d["份额折算"] = (((sr * nr) - 1).abs() < 0.1) & ((sr > 1.4) | (sr < 0.7)) & (gap == 1)
    d.loc[d["份额折算"], "份额变化"] = None
    d["估算净申购(亿元)"] = (d["份额变化"] * d["单位净值"] / 1e8).round(3)
    d["基金份额(亿份)"] = (d["基金份额"] / 1e8).round(3)
    return d[["日期", "基金代码", "品種", "基金份额(亿份)", "单位净值", "估算净申购(亿元)", "份额折算"]]


def clean_qvix(d, cal):
    """波指：去掉非交易日的列（來源偶爾有週日、補班日的列），同一品種同一天重複時留最後一筆"""
    d = d.copy()
    dates = d["date"].astype(str).str[:10]
    d = d[dates.isin(set(cal))]
    return d.drop_duplicates(["品種", "date"], keep="last").reset_index(drop=True)


def qvix_table():
    d = concat_named([(n, getattr(A(), f)) for n, f in QVIX])
    d["date"] = d["date"].astype(str).str[:10]
    return clean_qvix(d, trade_days("2015-01-01"))


CSI_SOURCE = {"中证全指": "000985", "中证红利": "000922"}


def trading_only(d, col, cal):
    """去掉非交易日的列（中證指數公司的資料偶爾有週末或休市日的列，例如 2005-01-01、2018-06-18）"""
    return d[d[col].astype(str).str[:10].isin(set(cal))].reset_index(drop=True)


def csindex_hist(code):
    d = A().index_hist_csindex(symbol=code, start_date="20050101", end_date=today().strftime("%Y%m%d"))
    return trading_only(d, "日期", trade_days("2004-12-01"))


def _csindex_daily(code):
    d = csindex_hist(code)
    return pd.DataFrame({"date": d["日期"].astype(str).str[:10], "open": d["开盘"], "high": d["最高"],
                         "low": d["最低"], "close": d["收盘"]})


def cn_index_daily():
    items = []
    for n, s_ in CN_IDX:
        if n in CSI_SOURCE:
            items.append((n, (lambda c=CSI_SOURCE[n]: _csindex_daily(c))))
        else:
            items.append((n, (lambda s=s_: A().index_zh_daily_sina(symbol=s))))
    return concat_named(items)


def us_macro_em():
    d = em_dc("RPT_ECONOMICVALUE_USA", sort="REPORT_DATE")
    d = d[["INDICATOR_NAME", "INDICATOR_ID", "REPORT_DATE", "PUBLISH_DATE", "VALUE", "PRE_VALUE"]]
    return d.rename(columns={"INDICATOR_NAME": "指标", "INDICATOR_ID": "指标代码", "REPORT_DATE": "日期",
                             "PUBLISH_DATE": "发布日", "VALUE": "数值", "PRE_VALUE": "前值"})


def _q(d):        # 2024-09-30 → 20240930
    return d.replace("-", "")


DATASETS = [
    # ---------------- 1 宏觀經濟
    ("cn_cpi", "CPI 居民消费价格", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_cpi(), ""),
    ("cn_ppi", "PPI 工业生产者出厂价格", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_ppi(), ""),
    ("cn_pmi", "PMI 官方（制造业／非制造业）", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_pmi(), ""),
    ("cn_pmi_cx_mfg", "财新制造业 PMI", "宏观经济", "财新（金十）", lambda: A().macro_china_pmi_manufacturing_cx(), ""),
    ("cn_pmi_cx_svc", "财新服务业 PMI", "宏观经济", "财新（金十）", lambda: A().macro_china_pmi_services_cx(), ""),
    ("cn_gdp", "GDP（季，累计）", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_gdp(), ""),
    ("cn_retail", "社会消费品零售总额", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_consumer_goods_retail(), ""),
    ("cn_ip", "工业增加值", "宏观经济", "国家统计局", lambda: A().macro_china_industrial_value_added(), ""),
    ("cn_fai", "固定资产投资", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_fixed_asset_investment(), ""),
    ("cn_trade", "进出口（海关）", "宏观经济", "海关总署（东方财富）", lambda: A().macro_china_customs_trade(), ""),
    ("cn_unemployment", "城镇调查失业率", "宏观经济", "国家统计局", lambda: A().macro_china_urban_unemployment(), "来源网站常拒绝程序存取，可能抓不到"),
    ("cn_consumer_conf", "消费者信心指数", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_consumer_confidence(), ""),
    ("cn_enterprise_boom", "企业景气／企业家信心指数（季）", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_enterprise_boom_index(), ""),
    ("cn_real_estate", "国房景气指数", "宏观经济", "国家统计局（金十）", lambda: A().macro_china_real_estate(), ""),
    ("cn_house_price", "70 城新建／二手住宅价格指数", "宏观经济", "国家统计局（东方财富）", lambda: A().macro_china_new_house_price(), "只有部分城市"),
    ("cn_electricity", "全社会用电量", "宏观经济", "中电联（新浪）", lambda: A().macro_china_society_electricity(), "克强指数之一"),
    ("cn_freight", "社会货运量／周转量", "宏观经济", "交通运输部（新浪）", lambda: A().macro_china_society_traffic_volume(), "克强指数之一；含铁路、公路、水运等分项"),
    ("cn_fdi", "外商直接投资", "宏观经济", "商务部（东方财富）", lambda: A().macro_china_fdi(), "来源 2023 年后停更"),
    # ---------------- 2 財政
    ("cn_fiscal_revenue", "财政收入（月）", "财政", "财政部（东方财富）", lambda: A().macro_china_fiscal_revenue(), ""),
    ("cn_tax", "全国税收收入（季）", "财政", "财政部（东方财富）", lambda: A().macro_china_national_tax_receipts(), ""),
    ("cn_local_bond", "地方政府债发行明细", "财政", "巨潮资讯", ("year", "2015",
        lambda y: A().bond_local_government_issue_cninfo(start_date=f"{y}0101", end_date=f"{y}1231"), 1), "含专项债、一般债；逐年抓"),
    # ---------------- 3 貨幣與流動性
    ("cn_money_supply", "M0／M1／M2 货币供应", "货币流动性", "人民银行（东方财富）", lambda: A().macro_china_money_supply_em(), "M1 2025 年起为新口径"),
    ("cn_social_financing", "社会融资规模增量", "货币流动性", "人民银行（商务部数据中心）", lambda: A().macro_china_social_financing(), ""),
    ("cn_new_credit", "新增信贷（月）", "货币流动性", "人民银行（东方财富）", lambda: A().macro_china_new_financial_credit(), ""),
    ("cn_rmb_loan", "新增人民币贷款（分项）", "货币流动性", "同花顺", lambda: A().macro_china_rmb_loan_ths(), "来源只提供最近约 30 个月"),
    ("cn_rmb_deposit", "新增人民币存款（分项，含居民存款）", "货币流动性", "同花顺", lambda: A().macro_china_rmb_deposit_ths(), "来源只提供最近约 30 个月；看「存款搬家」"),
    ("cn_lpr", "LPR 贷款市场报价利率", "货币流动性", "人民银行（东方财富）", lambda: A().interest_rate_lpr_em(), "2019 年前为贷款基准利率"),
    ("cn_rrr", "存款准备金率调整", "货币流动性", "人民银行（东方财富）", lambda: A().macro_china_reserve_requirement_ratio(), ""),
    ("cn_shibor", "SHIBOR", "货币流动性", "全国银行间同业拆借中心（金十）", lambda: A().interest_rate_shibor_js(), ""),
    ("cn_repo_fr", "银行间回购定盘利率 FR007／FDR007", "货币流动性", "中国货币网", ("year", "2006",
        lambda y: A().interest_rate_repo_hist_cm(start_date=f"{y}0101", end_date=f"{y}1231"), 1), "DR007 即 FDR007"),
    ("cn_us_bond_yield", "中美国债收益率（2/5/10/30 年）", "货币流动性", "东方财富", lambda: A().bond_zh_us_yield_em(start_date="19900101"), "含中美 10 年利差"),
    ("cn_bond_curves", "中债收益率曲线（国债、国开债、AAA／AA 企业债、中短期票据、商业银行债）", "货币流动性", "中国债券信息网", ("year", "2006",
        lambda y: A().bond_china_yield(start_date=f"{y}0101", end_date=f"{y}1231"), 1), "信用利差＝企业债收益率－同期限国债收益率"),
    ("cn_fx_gold_reserve", "外汇储备与黄金储备", "货币流动性", "外汇局（东方财富）", lambda: A().macro_china_fx_gold_em(), "看央行购金"),
    ("cn_bank_financing", "银行理财产品收益率", "货币流动性", "金十", lambda: A().macro_china_bank_financing(), ""),
    ("cn_fx_central_parity", "人民币汇率中间价（各币种）", "货币流动性", "国家外汇管理局", lambda: A().fx_central_parity_safe(), ""),
    # ---------------- 4 大宗商品
    ("gold_sge_au9999", "上海金 Au99.99 现货", "大宗商品", "上海黄金交易所", lambda: A().spot_hist_sge(symbol="Au99.99"), ""),
    ("silver_sge_ag9999", "上海银 Ag99.99 现货", "大宗商品", "上海黄金交易所", lambda: A().spot_hist_sge(symbol="Ag99.99"), ""),
    ("gold_sge_benchmark", "上海金基准价", "大宗商品", "上海黄金交易所", lambda: A().spot_golden_benchmark_sge(), ""),
    ("gold_etf_spdr", "SPDR 黄金 ETF 持仓", "大宗商品", "金十", lambda: A().fund_gold_etf_holding_js(), "全球最大黄金 ETF"),
    ("silver_etf_slv", "iShares 白银 ETF 持仓", "大宗商品", "金十", lambda: A().fund_silver_etf_holding_js(), ""),
    ("comex_gold_inventory", "COMEX 黄金库存", "大宗商品", "东方财富", lambda: A().futures_comex_inventory(symbol="黄金"), ""),
    ("cn_futures_main", "国内期货主力连续（金、银、原油、铜、钢、煤、农产品、股指、国债）", "大宗商品", "新浪",
        lambda: concat_named([(n, (lambda s=s: A().futures_main_sina(symbol=s))) for n, s in CN_FUT]), "主力合约连续，换月会有跳空"),
    ("foreign_futures", "海外期货（COMEX 金银铜、WTI、布伦特、天然气、A50 等）", "大宗商品", "新浪",
        lambda: concat_named([(n, (lambda s=s: A().futures_foreign_hist_sina(symbol=s))) for n, s in FOREIGN_FUT]), "多数从 2016 年起；WTI 从 1996 年起"),
    ("cn_oil_price", "国内成品油调价（汽油、柴油）", "大宗商品", "东方财富", lambda: A().energy_oil_hist(), ""),
    ("us_crude_production", "美国原油产量（周）", "大宗商品", "EIA（金十）", lambda: A().macro_usa_crude_production(), ""),
    ("cftc_commodity_nc", "CFTC 商品期货 非商业（投机）持仓", "大宗商品", "CFTC（金十）", lambda: A().futures_cftc_commodity_noncommercial_holding_js(), "黄金、原油等投机净多头"),
    ("cftc_commodity_c", "CFTC 商品期货 商业持仓", "大宗商品", "CFTC（金十）", lambda: A().futures_cftc_commodity_commercial_holding_js(), ""),
    ("cftc_currency_nc", "CFTC 外汇期货 非商业持仓", "大宗商品", "CFTC（金十）", lambda: A().futures_cftc_currency_noncommercial_holding_js(), ""),
    ("bdi", "BDI 波罗的海干散货指数", "大宗商品", "金十", lambda: A().macro_shipping_bdi(), ""),
    ("cn_commodity_index", "中国大宗商品价格指数", "大宗商品", "金十", lambda: A().macro_china_commodity_price_index(), ""),
    # ---------------- 5 國家隊／社保／長線資金
    ("holders_national_team", "国家队 十大流通股东持股（汇金、证金、梧桐树、诚通、国新等）", "国家队社保",
        "东方财富 十大流通股东", lambda: pd.concat([holders('(HOLDER_NEWTYPE="国家队")'),
                                              holders("(HOLDER_NAME in (" + ",".join(f'"{n}"' for n in NT_EXTRA) + "))")],
                                             ignore_index=True).drop_duplicates(), "每季财报公布；一家公司一期一列"),
    ("holders_nssf", "社保基金／养老金 十大流通股东持股", "国家队社保", "东方财富 十大流通股东",
        lambda: holders('(HOLDER_NEWTYPE="社保")'), "含全国社保基金各组合、基本养老保险基金"),
    ("holders_insurance", "保险资金 十大流通股东持股", "国家队社保", "东方财富 十大流通股东", lambda: holders('(HOLDER_NEWTYPE="保险")'), ""),
    ("holders_qfii", "QFII 十大流通股东持股", "国家队社保", "东方财富 十大流通股东", lambda: holders('(HOLDER_NEWTYPE="QFII")'), ""),
    ("inst_hold_nssf", "社保持仓汇总（每季每股）", "国家队社保", "东方财富 主力持仓", ("quarter", "2005-03-31",
        lambda d: A().stock_report_fund_hold(symbol="社保持仓", date=_q(d)), 2), ""),
    ("inst_hold_fund", "公募基金持仓汇总（每季每股）", "国家队社保", "东方财富 主力持仓", ("quarter", "2005-03-31",
        lambda d: A().stock_report_fund_hold(symbol="基金持仓", date=_q(d)), 2), ""),
    ("inst_hold_qfii", "QFII 持仓汇总（每季每股）", "国家队社保", "东方财富 主力持仓", ("quarter", "2005-03-31",
        lambda d: A().stock_report_fund_hold(symbol="QFII持仓", date=_q(d)), 2), ""),
    ("inst_hold_insurance", "保险持仓汇总（每季每股）", "国家队社保", "东方财富 主力持仓", ("quarter", "2005-03-31",
        lambda d: A().stock_report_fund_hold(symbol="保险持仓", date=_q(d)), 2), ""),
    ("inst_hold_broker", "券商持仓汇总（每季每股）", "国家队社保", "东方财富 主力持仓", ("quarter", "2005-03-31",
        lambda d: A().stock_report_fund_hold(symbol="券商持仓", date=_q(d)), 2), ""),
    ("holder_count", "上市公司股东户数（每季每股）", "国家队社保", "巨潮资讯", ("quarter", "2010-03-31",
        lambda d: A().stock_hold_num_cninfo(date=_q(d)), 2), "户数减少＝筹码集中"),
    ("index_members_hist", "沪深300／中证500／上证50 每月成分股快照", "国家队社保", "BaoStock", ("mstart", "2006-01-01",
        index_members_at, 1), "每月第一个交易日的成分股；用来算调入调出"),
    ("etf_shares_sse", "上交所 ETF 每日份额（看国家队买宽基 ETF）", "国家队社保", "上海证券交易所", ("day", "2012-05-28",
        lambda d: A().fund_etf_scale_sse(date=_q(d)), 3), "全部沪市 ETF；510300、510050、510500 等的份额暴增常被视为国家队进场"),
    ("etf_premium", "主要宽基 ETF 每日收盘价、单位净值、溢价率（14 档）", "国家队社保", "天天基金（净值）、新浪（价格）", etf_premium,
        "溢价率＝收盘价÷单位净值－1；大幅溢价代表场内抢筹"),
    ("etf_flows", "主要宽基 ETF 每日估算净申购（亿元）", "国家队社保", "由 ETF 份额 × 单位净值计算", lambda: etf_flows(),
        "份额变化 × 当天单位净值；份额表补得越完整，这张表越完整"),
    ("etf_shares_szse", "深交所 ETF 每日份额（规模前 40 大股票型 ETF，含 159919、159915 等宽基）", "国家队社保", "深圳证券交易所",
        ("quarter+", "2016-03-31", szse_etf_quarter, 2), "来源约从 2016 年起；份额单位：份；当季每次重抓"),
    # ---------------- 6 資金面
    ("margin_sse", "沪市融资融券", "资金面", "上交所（金十）", lambda: A().stock_margin_sse_js(), ""),
    ("margin_szse", "深市融资融券", "资金面", "深交所（金十）", lambda: A().stock_margin_szse_js(), ""),
    ("margin_account", "两融账户与担保比例", "资金面", "中国证券金融（东方财富）", lambda: A().stock_margin_account_info(), ""),
    ("hsgt_north", "北向资金（沪深股通）", "资金面", "东方财富", lambda: A().stock_hsgt_hist_em(symbol="北向资金"), "2024-08-19 起交易所不再公布每日净买入"),
    ("hsgt_south", "南向资金（港股通）", "资金面", "东方财富", lambda: A().stock_hsgt_hist_em(symbol="南向资金"), ""),
    ("new_investors", "新增投资者开户数（月）", "资金面", "中国结算（东方财富）", lambda: A().stock_account_statistics_em(), "中国结算 2023-08 后不再按月公布"),
    ("new_funds", "新发基金明细", "资金面", "东方财富", lambda: A().fund_new_found_em(), "可按成立日期汇总每月新发规模"),
    ("fund_position_stock", "股票型基金仓位估算", "资金面", "乐咕乐股", lambda: A().fund_stock_position_lg(), ""),
    ("fund_position_balance", "平衡混合型基金仓位估算", "资金面", "乐咕乐股", lambda: A().fund_balance_position_lg(), ""),
    ("restricted_release", "限售股解禁（每日汇总）", "资金面", "东方财富", ("year", "2005",
        lambda y: A().stock_restricted_release_summary_em(symbol="全部股票", start_date=f"{y}0101", end_date=f"{y}1231"), 2), "包含未来已排定的解禁"),
    ("block_trade", "大宗交易（每日汇总）", "资金面", "东方财富", lambda: A().stock_dzjy_sctj(), ""),
    ("repurchase", "上市公司回购明细", "资金面", "东方财富", lambda: A().stock_repurchase_em(), ""),
    ("pledge", "A 股股票质押（每周）", "资金面", "东方财富", lambda: A().stock_gpzy_profile_em(), ""),
    # ---------------- 7 估值與情緒
    ("a_pe", "全 A 市盈率（中位数／平均）", "估值情绪", "乐咕乐股", lambda: A().stock_a_ttm_lyr(), "含历史分位"),
    ("a_pb", "全 A 市净率", "估值情绪", "乐咕乐股", lambda: A().stock_a_all_pb(), "含历史分位"),
    ("index_pe", "主要指数市盈率（上证50、沪深300、中证500、中证1000 等）", "估值情绪", "乐咕乐股",
        lambda: concat_named([(n, (lambda n=n: A().stock_index_pe_lg(symbol=n))) for n in INDEX_PE]), ""),
    ("equity_bond_spread", "股债利差（沪深300）", "估值情绪", "乐咕乐股", lambda: A().stock_ebs_lg(), ""),
    ("buffett_index", "巴菲特指标（总市值／GDP）", "估值情绪", "乐咕乐股", lambda: A().stock_buffett_index_lg(), ""),
    ("dividend_yield", "A 股股息率", "估值情绪", "乐咕乐股", lambda: A().stock_a_gxl_lg(symbol="上证A股"), ""),
    ("below_net_asset", "破净股数量与占比", "估值情绪", "乐咕乐股", lambda: A().stock_a_below_net_asset_statistics(symbol="全部A股"), ""),
    ("congestion", "大盘拥挤度", "估值情绪", "乐咕乐股", lambda: A().stock_a_congestion_lg(), ""),
    ("high_low", "创新高／新低家数", "估值情绪", "乐咕乐股", lambda: A().stock_a_high_low_statistics(symbol="all"), "来源只提供最近约 2 年"),
    ("cb_index", "集思录可转债等权指数", "估值情绪", "集思录", lambda: A().bond_cb_index_jsl(), "来源只提供最近约 1 年"),
    ("cn_index_daily", "A 股主要指数日线", "估值情绪", "新浪；中证全指、中证红利用中证指数公司",
        lambda: cn_index_daily(), "新浪的中证全指停在 2016、中证红利停在 2019，改用中证指数公司（2005 年起，无成交量）"),
    # ---------------- 8 衍生品
    ("qvix", "中国波指（期权隐含波动率，50ETF／300ETF／1000 等）", "衍生品", "期权论坛",
        lambda: qvix_table(), "数字越大越恐慌；新指数上市前为空；已去掉非交易日与重复列"),
    ("option_stats_sse", "上交所 ETF 期权每日统计（认沽／认购比 PCR、成交、持仓）", "衍生品", "上海证券交易所", ("day", "2015-02-09",
        lambda d: A().option_daily_stats_sse(date=_q(d)), 3), "50ETF 期权 2015-02 上市；PCR 偏高＝避险需求强"),
    ("option_stats_szse", "深交所 ETF 期权每日统计（认沽／认购比 PCR、成交、持仓）", "衍生品", "深圳证券交易所", ("day", "2019-12-23",
        lambda d: A().option_daily_stats_szse(date=_q(d)), 3), "300ETF 期权 2019-12 上市"),
    ("cffex_rank", "中金所股指期货前 20 名会员持仓（IF/IH/IC/IM）", "衍生品", "中国金融期货交易所", ("day", "2010-04-16",
        lambda d: _cffex(d), 2), "可看中信等头部席位净空单变化"),
    # ---------------- 9 海外
    ("us_macro", "美国总经（CPI、核心 CPI、PPI、非农、失业率、ISM、零售、利率决议等 24 项）", "海外", "东方财富", us_macro_em, "长表：一列一个指标一期"),
    ("fred", "美国金融／流动性（PCE、初请、美联储资产负债表、TGA、逆回购、VIX、美债、美元、比特币等）", "海外", "FRED 圣路易斯联储",
        lambda: fred(list(FRED_IDS)), "长表：series 是 FRED 代码，中文名称见 Excel 目录"),
    ("us_initial_claims", "美国初领／续领失业金人数（每周，未季调）", "海外", "美国劳工部", us_initial_claims, "各州合计；1984 年起"),
    ("us_rrp", "美联储隔夜逆回购（ON RRP）余额", "海外", "纽约联储", us_rrp, "2013 年起；余额大＝市场资金多到没地方放"),
    ("us_fed_soma", "美联储持有证券（SOMA，资产负债表主体，每周）", "海外", "纽约联储", us_soma, "2003 年起；看 QE／QT"),
    ("us_tga", "美国财政部 TGA 账户余额（每日）", "海外", "美国财政部", us_tga, "余额上升＝从市场抽走流动性"),
    ("us_policy_rates_daily", "美国有效联邦基金利率 EFFR、SOFR（每日）", "海外", "纽约联储", us_nyfed_rates, ""),
    ("vix", "VIX 恐慌指数（日线）", "海外", "芝加哥期权交易所 CBOE", cboe_vix, "1990 年起"),
    ("dxy", "美元指数 DXY", "海外", "东方财富", lambda: em_kline("100.UDI", "美元指数"), ""),
    ("cb_policy_rates", "主要国家央行政策利率（月底，18 国／地区：美、欧、英、日、中、台、韩、港等）", "海外", "国际清算银行 BIS；台湾：中央银行开放资料",
        cb_rates_monthly, "1990 年起；一栏一个国家"),
    ("cb_rate_changes", "各国央行每一次升息／降息纪录（含上证、标普之后 20 日涨跌）", "海外", "国际清算银行 BIS",
        cb_rate_changes, "幅度单位：基点（0.25% = 25 基点）；日期为生效日；维持不到 7 天或小于 5 基点的变动不算"),
    ("global_macro", "欧洲、英国、日本、加拿大、澳洲主要总经（CPI、GDP、IFO、ZEW、失业率、零售、房价等 22 项）", "海外", "金十",
        global_macro, "长表；有发布日期；没有欧元区整体，用德国代表"),
    ("us_cpi_yoy", "美国 CPI 年率（含公布日）", "海外", "金十", lambda: A().macro_usa_cpi_yoy(), ""),
    ("japan_rate", "日本央行利率", "海外", "金十", lambda: A().macro_japan_bank_rate(), "日元套息交易"),
    ("japan_cpi", "日本 CPI 年率", "海外", "金十", lambda: A().macro_japan_cpi_yoy(), ""),
    ("global_index", "海外指数日线（标普、纳指、道指、中国金龙、恒指、国企、恒生科技）", "海外", "新浪",
        lambda: concat_named(list(GLOBAL_IDX)), ""),
    # ---------------- 10 產業
    ("industry_indicators", "行业指标（运价、钢铁、有色、汽车、手机出货、货运、用电、地产销售、保费等）", "产业", "东方财富 行业指数",
        industry_indicators, "长表：一列一个指标一期；Excel 里转成一栏一个指标"),
    ("csi_industry_daily", "中证全指一级行业指数日线（10 个行业，含市盈率）", "产业", "中证指数公司", csi_industry_daily,
        "申万日线在 GitHub 上连不到时的替代；2005 年起"),
    ("sw_industry_daily", "申万一级行业指数日线（31 个行业）", "产业", "申万宏源", lambda: sw_industry_daily(), "看行业轮动、强弱"),
    ("sw_industry_monthly", "申万行业月报（市盈率、市净率、股息率、涨跌幅）", "产业", "申万宏源", ("month", "2000-01-01",
        lambda d: A().index_analysis_monthly_sw(symbol="一级行业", date=_q(d)), 2), "每月最后一个交易日"),
    ("fund_industry_alloc", "公募基金行业配置（每季）", "产业", "巨潮资讯", ("quarter", "2007-03-31",
        lambda d: A().fund_report_industry_allocation_cninfo(date=_q(d)), 2), "看基金加减仓哪些行业"),
    ("cn_energy_index", "中国能源指数", "产业", "金十", lambda: A().macro_china_energy_index(), ""),
    # ---------------- 11 政策
    ("policy_uncertainty", "中国经济政策不确定性指数（EPU）与贸易政策不确定性（TPU）", "政策事件", "policyuncertainty.com",
        epu, "数字越高代表政策越不确定；学术界常用"),
    ("index_changes", "指数成分股调入／调出纪录（沪深300、中证500、上证50）", "国家队社保", "由每月成分股快照计算",
        lambda: index_changes(), "生效日取 BaoStock 的成分更新日；被动资金会在生效日前后被迫买卖"),
    # ---------------- 12 政策與大事件（放最後：要用上面抓到的上證指數計算市場反應）
    ("events", "历年重要政策与大事件（含上证当天及之后 5/20/60 日涨跌）", "政策事件", "人工整理 events.csv", lambda: events_table(),
        "可直接編輯 events.csv 增補；漲跌以事件前一交易日收盤為基準"),
]


EVENTS_CSV = Path(__file__).resolve().parent / "events.csv"


def events_table():
    """歷年重要政策／大事件（events.csv，人工整理）＋當天及之後 5／20／60 個交易日的上證指數漲跌"""
    ev = pd.read_csv(EVENTS_CSV, dtype=str).fillna("")
    try:
        con = sqlite3.connect(str(DB))
        px = pd.read_sql("select 期間 as d, close from cn_index_daily where 品種='上证指数' order by 期間", con)
        con.close()
    except Exception:  # noqa: BLE001
        px = pd.DataFrame(columns=["d", "close"])
    if len(px):
        px["close"] = px["close"].astype(float)
        days, closes = list(px["d"]), list(px["close"])
        import bisect
        out = {k: [] for k in ("上证收盘", "当天涨跌%", "之后5日%", "之后20日%", "之后60日%", "计算基准日")}
        for d in ev["日期"]:
            i = bisect.bisect_left(days, d)          # 事件當天（非交易日就用下一個交易日）
            if i >= len(days) or i == 0:
                for k in out:
                    out[k].append(None)
                continue
            base = closes[i - 1]                    # 事件前一個交易日收盤
            out["计算基准日"].append(days[i - 1])
            out["上证收盘"].append(closes[i])
            out["当天涨跌%"].append(round((closes[i] / base - 1) * 100, 2))
            for n, k in ((5, "之后5日%"), (20, "之后20日%"), (60, "之后60日%")):
                j = i + n - 1
                out[k].append(round((closes[j] / base - 1) * 100, 2) if j < len(days) else None)
        for k, v in out.items():
            ev[k] = v
    return ev


def _cffex(d):
    df = A().futures_rank_table_cffex(date=_q(d))
    if df is None or df.empty:
        return df
    return df[df["variety"].isin(["IF", "IH", "IC", "IM"])]


# ======================================================================== 資料庫
def open_db(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("""create table if not exists _datasets(
        id text primary key, name text, category text, source text, kind text, rows integer,
        first text, last text, last_ok text, last_error text, note text)""")
    con.execute("create table if not exists _progress(id text, period text, rows integer, at text, primary key(id, period))")
    return con


def _clean(df):
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: v.isoformat() if isinstance(v, (date, datetime)) else v)
        elif str(df[c].dtype).startswith("datetime"):
            df[c] = df[c].dt.strftime("%Y-%m-%d")
    return df


def _drop_dup_cols(df):
    return df.loc[:, ~pd.Index(df.columns).duplicated()]


# 官方公布時間（估計，偏保守＝寧可晚不可早）：(期間結束後第幾個月, 幾號)
#   回測用「可用日」才不會偷看未來：例如 2026-10 的 CPI 要到 2026-11-15 前後才公布
RELEASE_RULES = {
    "cn_cpi": (1, 15), "cn_ppi": (1, 15), "cn_pmi": (1, 1), "cn_pmi_cx_mfg": (1, 3), "cn_pmi_cx_svc": (1, 5),
    "cn_gdp": (1, 20), "cn_retail": (1, 18), "cn_ip": (1, 18), "cn_fai": (1, 18), "cn_trade": (1, 15),
    "cn_consumer_conf": (2, 1), "cn_enterprise_boom": (2, 1), "cn_real_estate": (1, 18), "cn_house_price": (1, 18),
    "cn_electricity": (1, 22), "cn_freight": (1, 22), "cn_fdi": (1, 20), "cn_fiscal_revenue": (1, 25), "cn_tax": (1, 25),
    "cn_money_supply": (1, 15), "cn_social_financing": (1, 15), "cn_new_credit": (1, 15), "cn_rmb_loan": (1, 15),
    "cn_rmb_deposit": (1, 15), "cn_fx_gold_reserve": (1, 8), "cn_bank_financing": (1, 15),
    "industry_indicators": (1, 25), "policy_uncertainty": (1, 10), "japan_cpi": (1, 25),
    "us_cpi_yoy": (1, 15), "global_macro": (1, 25), "us_macro": (1, 15),
}


def release_date(period, rule):
    """期間（YYYY-MM／YYYYQn／YYYY-MM-DD 代表的那個月）→ 估計公布日 YYYY-MM-DD"""
    if not isinstance(period, str) or not period:
        return None
    m = re.match(r"^(\d{4})Q(\d)$", period)
    if m:
        y, mo = int(m.group(1)), int(m.group(2)) * 3
    else:
        m = re.match(r"^(\d{4})-(\d{2})", period)
        if not m:
            return None
        y, mo = int(m.group(1)), int(m.group(2))
    k, day = rule
    mo += k
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y}-{mo:02d}-{day:02d}"


# 來源本身有「公布日」欄的表：只在落在「期間開始後 200 天內」時才採用（來源偶爾整欄填同一天，例如日本利率）
SOURCE_RELEASE = {"us_cpi_yoy": "发布日期", "japan_cpi": "发布日期", "global_macro": "发布日期", "us_macro": "发布日"}
# 金十的預告列（還沒公布、數值是空的）一律不收；這些表只留有數值的列
NONNULL = {"us_cpi_yoy": "现值", "japan_cpi": "现值", "japan_rate": "现值", "global_macro": "现值", "us_macro": "数值"}


def period_start(p):
    """期間 → 該期間第一天（YYYY-MM-DD）"""
    if not isinstance(p, str) or not p:
        return None
    m = re.match(r"^(\d{4})Q(\d)$", p)
    if m:
        return f"{m.group(1)}-{(int(m.group(2)) - 1) * 3 + 1:02d}-01"
    if re.match(r"^\d{4}-\d{2}-\d{2}", p):
        return p[:10]
    if re.match(r"^\d{4}-\d{2}$", p):
        return p + "-01"
    return None


def add_release(con, sid, df):
    """加上「公布日(估)」「首次抓到日」「可用日」三欄（只有月／季資料）。
    首次抓到日：這個期間第一次出現在資料裡的日期（從開始記錄之後才有；歷史期間沒有，用公布日(估)）"""
    if sid not in RELEASE_RULES or "期間" not in df.columns:
        return df
    con.execute("create table if not exists _first_seen(id text, period text, seen text, primary key(id, period))")
    existed = con.execute("select 1 from sqlite_master where type='table' and name=?", (sid,)).fetchone()
    if existed:
        old = {r[0] for r in con.execute(f'select distinct "期間" from "{sid}"')}
        new = sorted(set(df["期間"].dropna()) - old)
        con.executemany("insert or ignore into _first_seen values(?,?,?)", [(sid, p, today().isoformat()) for p in new])
    seen = dict(con.execute("select period, seen from _first_seen where id=?", (sid,)).fetchall())
    df = df.copy()
    pos = list(df.columns).index("期間") + 1
    est = df["期間"].map(lambda p: release_date(p, RELEASE_RULES[sid]))
    src = SOURCE_RELEASE.get(sid)
    if src and src in df.columns:
        sd = pd.to_datetime(df[src], errors="coerce")
        st = pd.to_datetime(df["期間"].map(period_start), errors="coerce")
        ok = sd.notna() & st.notna() & (sd >= st) & (sd <= st + pd.Timedelta(days=200))
        est = est.where(~ok, sd.dt.strftime("%Y-%m-%d"))
    if sid == "industry_indicators" and "指标" in df.columns:
        # 混合頻率：近 400 天資料 ≥100 筆的是日頻（運價、價格指數），當天就看得到；其餘是月頻，套「次月 25 日」
        dd = pd.to_datetime(df["期間"], errors="coerce")
        recent = dd >= dd.max() - pd.Timedelta(days=400)
        daily = df[recent].groupby("指标").size()
        daily = set(daily[daily >= 100].index)
        est = est.where(~df["指标"].isin(daily), df["期間"].str[:10])
    fs = df["期間"].map(seen.get)
    df.insert(pos, "公布日(估)", est)
    df.insert(pos + 1, "首次抓到日", fs)
    df.insert(pos + 2, "可用日", fs.where(fs.notna(), est))
    return df


def write_full(con, sid, df):
    df = _drop_dup_cols(_clean(add_period(df)))
    if sid in NONNULL and NONNULL[sid] in df.columns:
        df = df[df[NONNULL[sid]].notna()]                 # 還沒公布的預告列
    df = df.drop_duplicates().reset_index(drop=True)      # 來源偶爾同一天重複列（巴菲特指標、破淨股、日本利率…）
    df = add_release(con, sid, df)
    tmp = f"_new_{sid}"
    con.execute(f'drop table if exists "{tmp}"')
    df.to_sql(tmp, con, index=False)
    con.execute(f'drop table if exists "{sid}"')
    con.execute(f'alter table "{tmp}" rename to "{sid}"')


def write_period(con, sid, period, df):
    """逐期數據：先刪這一期舊的，再寫入；新欄位自動補上"""
    df = _drop_dup_cols(_clean(df))
    df.insert(0, "_期", period)
    exists = con.execute("select 1 from sqlite_master where type='table' and name=?", (sid,)).fetchone()
    if exists:
        have = [r[1] for r in con.execute(f'pragma table_info("{sid}")')]
        for c in df.columns:
            if c not in have:
                con.execute(f'alter table "{sid}" add column "{c}"')
        con.execute(f'delete from "{sid}" where "_期"=?', (period,))
    df.to_sql(sid, con, index=False, if_exists="append")


def mark(con, sid, name, cat, src, kind, note, ok, err=None):
    n = first = last = None
    if con.execute("select 1 from sqlite_master where type='table' and name=?", (sid,)).fetchone():
        cols = [r[1] for r in con.execute(f'pragma table_info("{sid}")')]
        pc = "期間" if "期間" in cols else ("_期" if "_期" in cols else None)
        if pc:
            n, first, last = con.execute(f'select count(*), min("{pc}"), max("{pc}") from "{sid}"').fetchone()
        else:
            n = con.execute(f'select count(*) from "{sid}"').fetchone()[0]
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    con.execute("""insert into _datasets values(?,?,?,?,?,?,?,?,?,?,?)
        on conflict(id) do update set name=excluded.name, category=excluded.category, source=excluded.source,
        kind=excluded.kind, rows=excluded.rows, first=excluded.first, last=excluded.last,
        last_ok=coalesce(excluded.last_ok, _datasets.last_ok), last_error=excluded.last_error, note=excluded.note""",
                (sid, name, cat, src, kind, n, first, last, now if ok else None, None if ok else err, note))
    con.commit()


# ======================================================================== 主流程
def quality_report(con):
    """逐表自動檢查：完全重複的列、期間在未來的列。結果寫進 _quality 表，回傳 [(表, 問題, 筆數)]"""
    con.execute("create table if not exists _quality(tbl text, issue text, rows integer, checked_at text)")
    con.execute("delete from _quality")
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    out = []
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like '\\_%' escape '\\'")]
    for t in tabs:
        cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
        allc = ",".join(f'"{c}"' for c in cols)
        dup = con.execute(f'select coalesce(sum(n - 1), 0) from (select count(*) n from "{t}" group by {allc} having n > 1)').fetchone()[0]
        if dup:
            out.append((t, "完全重複的列", int(dup)))
        if "期間" in cols:
            nul = con.execute(f'select count(*) from "{t}" where "期間" is null').fetchone()[0]
            if nul:
                out.append((t, "期間是空的（日期格式沒認出來）", int(nul)))
            fut = con.execute(f'select count(*) from "{t}" where "期間" glob "[12][0-9][0-9][0-9]-[0-9][0-9]*" and substr("期間",1,10) > ? '
                              f'and length("期間") = 10', (today().isoformat(),)).fetchone()[0]
            fut_m = con.execute(f'select count(*) from "{t}" where length("期間") = 7 and "期間" > ?', (today().isoformat()[:7],)).fetchone()[0]
            if fut + fut_m:
                out.append((t, "期間在未來", int(fut + fut_m)))
    con.executemany("insert into _quality values(?,?,?,?)", [(a, b, c, now) for a, b, c in out])
    con.commit()
    return out


GROUP_COLS = ("品種", "指标", "指数", "基金代码", "利率", "series", "代码")


def data_summary(con, max_len=260):
    """每張資料表一行摘要（筆數、起訖、最新一列的數值、最新日有幾個品種、空值多的欄位），印在執行紀錄裡，
    讓人不用打開 Drive 上的資料庫，也能核對實際上傳的內容"""
    lines = []
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like '\\_%' escape '\\' order by name")]
    for t in tabs:
        cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
        n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
        if n == 0:
            lines.append(f"摘要 {t}: 空表")
            continue
        pc = "期間" if "期間" in cols else ("_期" if "_期" in cols else None)
        parts = [f"{n} 筆"]
        if pc:
            first, last = con.execute(f'select min("{pc}"), max("{pc}") from "{t}"').fetchone()
            parts.append(f"{first}～{last}")
        grp = next((g for g in GROUP_COLS if g in cols), None)
        if grp and pc:
            total, at_last = con.execute(f'select count(distinct "{grp}"), '
                                         f'count(distinct case when "{pc}" = ? then "{grp}" end) from "{t}"', (last,)).fetchone()
            parts.append(f"{total} 個{grp}，最新日有 {at_last} 個")
        # 最新一列的數值欄（最多 4 個）
        order = f'order by "{pc}" desc' if pc else "order by rowid desc"
        row = con.execute(f'select * from "{t}" {order} limit 1').fetchone()
        vals = []
        for c, v in zip(cols, row):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and c not in (pc, "州数") and len(vals) < 4:
                vals.append(f"{c}={v:.6g}")
        if vals:
            parts.append("最新：" + "，".join(vals))
        # 空值超過一半的欄位
        nulls = con.execute("select " + ",".join(f'sum("{c}" is null)' for c in cols) + f' from "{t}"').fetchone()
        sparse = [f"{c}({(k or 0) / n:.0%})" for c, k in zip(cols, nulls) if (k or 0) / n > 0.5 and c != "首次抓到日"]
        if sparse:
            parts.append("空值過半：" + "、".join(sparse[:4]))
        lines.append((f"摘要 {t}: " + "；".join(parts))[:max_len])
    return lines


def out_of_time():
    return _deadline is not None and time.monotonic() > _deadline


def run_full(con, ds):
    sid, name, cat, src, fn, note = ds
    t = time.time()
    try:
        df = fn()
        if df is None or len(df) == 0:
            raise RuntimeError("沒有資料")
        write_full(con, sid, df)
        mark(con, sid, name, cat, src, "整張", note, True)
        r = con.execute("select rows, first, last from _datasets where id=?", (sid,)).fetchone()
        log(f"✓ {sid} {name}：{r[0]} 筆 {r[1] or ''}～{r[2] or ''}（{time.time() - t:.0f} 秒）")
        return True
    except Exception as e:  # noqa: BLE001
        mark(con, sid, name, cat, src, "整張", note, False, f"{type(e).__name__}: {str(e)[:200]}")
        log(f"✗ {sid} {name}：{type(e).__name__} {str(e)[:120]}")
        return False


def run_periodic(con, ds):
    sid, name, cat, src, (unit, start, fn, refresh), note = ds
    plist = {"quarter": quarter_ends, "day": trade_days, "year": years, "month": sw_months,
             "mstart": month_starts, "quarter+": quarter_ends_cur}[unit](start)
    done = {r[0] for r in con.execute("select period from _progress where id=?", (sid,))}
    recent = set(plist[-refresh:]) if refresh else set()
    todo = [p for p in plist if p not in done or p in recent]
    # 先抓最新的幾期（確保日常更新），再從舊到新補歷史
    todo = [p for p in todo if p in recent] + [p for p in todo if p not in recent]
    n_ok = n_fail = 0
    err = None
    for p in todo:
        if out_of_time():
            break
        try:
            try:
                df = fn(p)
            except TypeError:       # akshare 遇到「這一期來源沒有資料」會丟 TypeError（例如 2005～2009 年的解禁），當作空的
                df = None
            rows = 0 if df is None else len(df)
            if rows:
                write_period(con, sid, p, df)
            # 空的期間（沒有交易、還沒公布）：過了一段時間才記為完成，避免太早標記
            age_ok = unit != "day" or p <= (today() - timedelta(days=5)).isoformat()
            if rows or age_ok:
                con.execute("insert or replace into _progress values(?,?,?,?)",
                            (sid, p, rows, datetime.now(TZ).strftime("%Y-%m-%d %H:%M")))
            con.commit()
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            err = f"{p}: {type(e).__name__}: {str(e)[:150]}"
            if n_fail >= 8 and n_ok == 0:
                break
            time.sleep(3)
        time.sleep(0.6)
    left = len([p for p in plist if p not in {r[0] for r in con.execute("select period from _progress where id=?", (sid,))}])
    extra = f"；還有 {left} 期沒抓，下次繼續" if left else ""
    mark(con, sid, name, cat, src, f"逐{ {'quarter': '季', 'day': '日', 'year': '年', 'month': '月', 'mstart': '月', 'quarter+': '季'}[unit] }", note + extra if note else extra.lstrip("；"),
         n_fail == 0, err)
    log(f"{'✓' if n_fail == 0 else '△'} {sid} {name}：本次 {n_ok} 期成功、{n_fail} 期失敗{extra}")
    return n_fail == 0


POST_IDS = {"index_changes", "etf_flows"}


def run(only=None, budget_min=BUDGET_MIN, db=DB):
    global _deadline
    _deadline = time.monotonic() + budget_min * 60
    con = open_db(db)
    ok = bad = 0
    sel = [d for d in DATASETS if not only or d[0] in only]
    post = [d for d in sel if d[0] in POST_IDS]                 # 要用到逐期數據的，最後才算
    full = [d for d in sel if callable(d[4]) and d[0] not in POST_IDS]
    per = [d for d in sel if not callable(d[4])]
    for d in full:            # 一般數據都很快，先全部做完
        ok, bad = (ok + 1, bad) if run_full(con, d) else (ok, bad + 1)
    for d in per:             # 逐期數據：在時間預算內盡量補
        if out_of_time():
            log(f"時間到，{d[0]} 留到下次")
            continue
        ok, bad = (ok + 1, bad) if run_periodic(con, d) else (ok, bad + 1)
    for d in post:
        ok, bad = (ok + 1, bad) if run_full(con, d) else (ok, bad + 1)
    con.execute("vacuum")
    con.close()
    return ok, bad


# ======================================================================== 匯出 Excel
def export(db, outdir, max_rows=300_000):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    meta = pd.read_sql("select * from _datasets", con)
    order = {d[0]: i for i, d in enumerate(DATASETS)}
    meta["_o"] = meta["id"].map(order).fillna(999)
    meta = meta.sort_values("_o").drop(columns="_o")
    toc = meta.rename(columns={"id": "表名", "name": "数据", "category": "分类", "source": "来源", "kind": "方式",
                               "rows": "笔数", "first": "起", "last": "迄", "last_ok": "最后成功", "last_error": "最后错误",
                               "note": "备注"})
    toc.insert(1, "Excel 页", "")
    fred_names = pd.DataFrame([{"FRED 代码": k, "中文名称": v} for k, v in FRED_IDS.items()])
    path = outdir / "股市相关数据_历年.xlsx"
    skipped = []
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        toc.to_excel(xw, sheet_name="目录", index=False)
        fred_names.to_excel(xw, sheet_name="FRED代码对照", index=False)
        for i, r in meta.iterrows():
            sid = r["id"]
            if not con.execute("select 1 from sqlite_master where type='table' and name=?", (sid,)).fetchone():
                continue
            n = con.execute(f'select count(*) from "{sid}"').fetchone()[0]
            if n > max_rows:
                skipped.append((sid, n))
                toc.loc[toc["表名"] == sid, "Excel 页"] = f"（{n} 笔，太大，只在 macro.db／CSV）"
                pd.read_sql(f'select * from "{sid}"', con).to_csv(outdir / f"{sid}.csv.gz", index=False, encoding="utf-8-sig")
                continue
            df = pd.read_sql(f'select * from "{sid}"', con)
            if sid == "industry_indicators":
                df = df.pivot_table(index="期間", columns="指标", values="数值").reset_index()
            elif sid == "global_macro":
                df = df.pivot_table(index="发布日期", columns="指标", values="现值", aggfunc="last").reset_index()
            elif sid == "policy_uncertainty":
                df = df.pivot_table(index="期間", columns="指数", values="数值").reset_index()
            if sid in ("fred", "us_macro"):          # 長表轉寬表，一欄一個指標
                if sid == "fred":
                    df = df.pivot_table(index="date", columns="series", values="value").rename(columns=FRED_IDS)
                else:
                    df = df.pivot_table(index="期間", columns="指标", values="数值")
                df = df.reset_index()
            sheet = re.sub(r"[\[\]:*?/\\]", "", sid)[:31]
            df.to_excel(xw, sheet_name=sheet, index=False)
            toc.loc[toc["表名"] == sid, "Excel 页"] = sheet
        toc.to_excel(xw, sheet_name="目录", index=False)
    con.close()
    return path, skipped


def main():
    ap = argparse.ArgumentParser(description="股市相關數據（歷年）")
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--export", default="")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--budget", type=float, default=BUDGET_MIN, help="最多跑幾分鐘")
    a = ap.parse_args()
    if a.list:
        for d in DATASETS:
            print(f"{d[2]}\t{d[0]}\t{d[1]}")
        print(f"共 {len(DATASETS)} 種")
        return
    if a.export:
        p, sk = export(Path(a.db), a.export)
        print(f"已匯出 {p}" + (f"；太大另存 CSV：{sk}" if sk else ""))
        return
    only = {x for x in a.only.split(",") if x} or None
    ok, bad = run(only, a.budget, Path(a.db))
    print(f"::notice title=股市相關數據::成功 {ok} 種、失敗 {bad} 種")


if __name__ == "__main__":
    main()
