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
    m = re.match(r"^(\d{4})年第([\d\-]+)季度", s)
    if m:
        return f"{m.group(1)}{_CN_Q.get(m.group(2), 'Q' + m.group(2)[-1])}"
    m = re.match(r"^(\d{4})年(\d{1,2})月", s) or re.match(r"^(\d{4})[.\-](\d{1,2})$", s) or re.match(r"^(\d{4})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def add_period(df, col=None):
    """在最前面加「期間」欄並由舊到新排序；col＝日期欄（不給就自動找第一個認得出日期的欄）"""
    if df is None or df.empty:
        return df
    cols = [col] if col else list(df.columns)
    for c in cols:
        p = df[c].map(norm_period)
        if p.notna().mean() > 0.8:
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


def fred(ids):
    """FRED（聖路易聯儲）：多個序列合成一張長表 series, date, value"""
    out = []
    for sid in ids:
        r = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": sid}, timeout=60, tries=2)
        d = pd.read_csv(io.StringIO(r.text))
        d.columns = ["date", "value"]
        d["value"] = pd.to_numeric(d["value"], errors="coerce")
        d = d.dropna()
        d.insert(0, "series", sid)
        out.append(d)
        time.sleep(0.5)
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
    """[(名稱, 函式)] → 一張表，多一欄「品種」"""
    out = []
    for name, fn in items:
        d = fn()
        d.insert(0, "品種", name)
        out.append(d)
        time.sleep(0.5)
    return pd.concat(out, ignore_index=True)


# ======================================================================== 逐期抓的期間清單
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
               ("CBOT 大豆", "S"), ("日经225", "NKD"))
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
    d = em_dc("RPT_F10_EH_FREEHOLDERS", flt, cols=",".join(HOLDER_COLS), sort="END_DATE")
    return d.rename(columns=HOLDER_COLS)


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
    ("etf_shares_sse", "上交所 ETF 每日份额（看国家队买宽基 ETF）", "国家队社保", "上海证券交易所", ("day", "2012-05-28",
        lambda d: A().fund_etf_scale_sse(date=_q(d)), 3), "全部沪市 ETF；510300、510050、510500 等的份额暴增常被视为国家队进场"),
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
    ("cn_index_daily", "A 股主要指数日线", "估值情绪", "新浪",
        lambda: concat_named([(n, (lambda s=s: A().index_zh_daily_sina(symbol=s))) for n, s in CN_IDX]), ""),
    # ---------------- 8 衍生品
    ("qvix", "中国波指（期权隐含波动率，50ETF／300ETF／1000 等）", "衍生品", "期权论坛",
        lambda: concat_named([(n, getattr(A(), f)) for n, f in QVIX]), "数字越大越恐慌"),
    ("cffex_rank", "中金所股指期货前 20 名会员持仓（IF/IH/IC/IM）", "衍生品", "中国金融期货交易所", ("day", "2010-04-16",
        lambda d: _cffex(d), 2), "可看中信等头部席位净空单变化"),
    # ---------------- 9 海外
    ("us_macro", "美国总经（CPI、核心 CPI、PPI、非农、失业率、ISM、零售、利率决议等 24 项）", "海外", "东方财富", us_macro_em, "长表：一列一个指标一期"),
    ("fred", "美国金融／流动性（PCE、初请、美联储资产负债表、TGA、逆回购、VIX、美债、美元、比特币等）", "海外", "FRED 圣路易斯联储",
        lambda: fred(list(FRED_IDS)), "长表：series 是 FRED 代码，中文名称见 Excel 目录"),
    ("dxy", "美元指数 DXY", "海外", "东方财富", lambda: em_kline("100.UDI", "美元指数"), ""),
    ("us_cpi_yoy", "美国 CPI 年率（含公布日）", "海外", "金十", lambda: A().macro_usa_cpi_yoy(), ""),
    ("japan_rate", "日本央行利率", "海外", "金十", lambda: A().macro_japan_bank_rate(), "日元套息交易"),
    ("japan_cpi", "日本 CPI 年率", "海外", "金十", lambda: A().macro_japan_cpi_yoy(), ""),
    ("global_index", "海外指数日线（标普、纳指、道指、中国金龙、恒指、国企、恒生科技）", "海外", "新浪",
        lambda: concat_named(list(GLOBAL_IDX)), ""),
    # ---------------- 10 政策與大事件（放最後：要用上面抓到的上證指數計算市場反應）
    ("events", "历年重要政策与大事件（含上证当天及之后 5/20/60 日涨跌）", "政策事件", "人工整理 events.csv", events_table,
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


def write_full(con, sid, df):
    df = _drop_dup_cols(_clean(add_period(df)))
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
    plist = {"quarter": quarter_ends, "day": trade_days, "year": years}[unit](start)
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
            df = fn(p)
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
    mark(con, sid, name, cat, src, f"逐{ {'quarter': '季', 'day': '日', 'year': '年'}[unit] }", note + extra if note else extra.lstrip("；"),
         n_fail == 0, err)
    log(f"{'✓' if n_fail == 0 else '△'} {sid} {name}：本次 {n_ok} 期成功、{n_fail} 期失敗{extra}")
    return n_fail == 0


def run(only=None, budget_min=BUDGET_MIN, db=DB):
    global _deadline
    _deadline = time.monotonic() + budget_min * 60
    con = open_db(db)
    ok = bad = 0
    sel = [d for d in DATASETS if not only or d[0] in only]
    full = [d for d in sel if callable(d[4])]
    per = [d for d in sel if not callable(d[4])]
    for d in full:            # 一般數據都很快，先全部做完
        ok, bad = (ok + 1, bad) if run_full(con, d) else (ok, bad + 1)
    for d in per:             # 逐期數據：在時間預算內盡量補
        if out_of_time():
            log(f"時間到，{d[0]} 留到下次")
            continue
        ok, bad = (ok + 1, bad) if run_periodic(con, d) else (ok, bad + 1)
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
