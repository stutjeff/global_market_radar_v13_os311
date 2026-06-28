# -*- coding: utf-8 -*-
"""
全球市場雷達 v13.5｜總控 + 產業輪動 + Reddit 題材整合版

V13 重點：
1. 警報權重分數：輸出 0~100 市場風險總分與等級。
2. 昨天 vs 今天：追蹤 VIX、HYG/LQD、日圓、美債10Y、台股融資、核心ETF 的變化速度。
3. 為什麼是這個戰鬥模式：每次通知固定列出原因與結論。
4. 改變模式條件：輸出下一步觀察，告訴你什麼條件成立要轉模式。
5. R模式：Rebound / 衛星反攻模式，避免只會逃命、不知道何時回戰場。
6. MOVE 指數：債券波動率，補強美債壓力偵測。
7. 台股市場廣度/貪婪 proxy：用 RSP/SPY、IWM/SPY、QQQ/RSP、核心ETF偏離均線判斷過熱與廣度。

注意：這是風控儀表板，不是自動駕駛。它提高你在危險來臨前降波動的機率，但不能保證每次崩盤前都準確預警。
"""

from __future__ import annotations

import os
os.makedirs("storage", exist_ok=True)
os.makedirs("logs", exist_ok=True)
os.makedirs("data_cache", exist_ok=True)
import json
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import pytz
import yfinance as yf

TZ = pytz.timezone("Asia/Taipei")
TODAY = datetime.now(TZ)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

OS31_STATE_FILE = "storage/os31_state.json"
OS31_MIN_433_HOLD_WEEKS = 8
MARGIN_HISTORY_FILE = "storage/tw_margin_history.csv"
TPEX_MARGIN_HISTORY_FILE = "storage/tpex_margin_history.csv"
PRIVATE_CREDIT_AUX_TICKERS = ["BKLN", "SRLN", "BIZD", "JAAA", "JBBB", "ARCC", "BXSL", "OBDC", "MAIN", "FSK"]

# Data Health Gate:
# A-grade core data. If these are too stale, OS 3.1.1 will not execute a mode switch.
DATA_HEALTH_CORE_TICKERS = ["^VIX", "QQQ", "HYG", "LQD", "00662.TW", "00670L.TW", "00865B.TW"]
DATA_HEALTH_SECONDARY_TICKERS = ["SMH", "SOXX", "^TWII", "SPY", "RSP", "IWM", "SHY", "JPY=X", "^TNX"]
DATA_HEALTH_MAX_CORE_STALE_DAYS = 3
DATA_HEALTH_MAX_MARGIN_STALE_DAYS = 5

TICKERS = {
    # 全球市場核心
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "RSP": "S&P 500 Equal Weight",
    "IWM": "Russell 2000",
    "SMH": "Semiconductor ETF",
    "SOXX": "Semiconductor ETF 2",
    "EWT": "Taiwan ETF",
    "EWJ": "Japan ETF",

    # 壓力與避險
    "^VIX": "VIX",
    "^MOVE": "MOVE Bond Volatility",
    "^TNX": "US 10Y Yield x10",
    "^TYX": "US 30Y Yield x10",
    "TLT": "US Long Treasury ETF",
    "IEF": "US 7-10Y Treasury ETF",
    "GLD": "Gold ETF",
    "UUP": "US Dollar ETF",
    "JPY=X": "USDJPY",
    "CL=F": "WTI Oil Futures",

    # 信用利差 proxy
    "HYG": "High Yield Bond ETF",
    "JNK": "High Yield Bond ETF 2",
    "LQD": "Investment Grade Bond ETF",

    # 使用者核心ETF
    "00662.TW": "00662 Nasdaq ETF",
    "00670L.TW": "00670L Nasdaq 2x ETF",
    "00865B.TW": "00865B US Treasury ETF",
    "^TWII": "Taiwan Weighted Index",

    # V12：美股產業輪動 ETF
    "XLK": "US Technology ETF",
    "XLU": "US Utilities ETF",
    "XLP": "US Consumer Staples ETF",
    "XLI": "US Industrials ETF",
    "XLE": "US Energy ETF",
    "XLF": "US Financials ETF",
    "ARKK": "Innovation / Speculative Growth ETF",

    # V12：台股產業輪動 proxy
    "2382.TW": "廣達",
    "3231.TW": "緯創",
    "2356.TW": "英業達",
    "2308.TW": "台達電",
    "1503.TW": "士電",
    "1513.TW": "中興電",
    "1519.TW": "華城",
    "8473.TW": "山林水",
    "8341.TW": "日友",
    "8422.TW": "可寧衛",
    "2330.TW": "台積電",
    "3711.TW": "日月光投控",
    "2467.TW": "志聖",
    "4958.TW": "臻鼎-KY",
    "3037.TW": "欣興",
    "3044.TW": "健鼎",
    "3324.TWO": "雙鴻",
    "3017.TW": "奇鋐",
    "3653.TW": "健策",
    "2912.TW": "統一超",
    "1229.TW": "聯華",
    "1210.TW": "大成",
}

# 戰鬥模式比例：程式會自動換算成百分比
BATTLE_MODES = {
    "514": {"00662.TW": 5, "00670L.TW": 1, "00865B.TW": 4},
    "433": {"00662.TW": 4, "00670L.TW": 3, "00865B.TW": 3},
    # 452 是疑似危機/假訊號緩衝，不是 4:5:2。
    # 依使用者原始邏輯：45% / 25% / 30%。
    "452": {"00662.TW": 45, "00670L.TW": 25, "00865B.TW": 30},
}


def safe_float(x, default=np.nan) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.replace(",", "").replace("%", "").strip()
        return float(x)
    except Exception:
        return default


def pct(x: float) -> str:
    if pd.isna(x) or np.isinf(x):
        return "N/A"
    return f"{x * 100:.2f}%"


def num(x: float, digits: int = 2) -> str:
    if pd.isna(x) or np.isinf(x):
        return "N/A"
    return f"{x:.{digits}f}"


def bp(x: float) -> str:
    if pd.isna(x) or np.isinf(x):
        return "N/A"
    return f"{x * 100:.0f}bp"


def normalize_mode(mode: Dict[str, float]) -> Dict[str, float]:
    total = sum(mode.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in mode.items()}


def series(df: pd.DataFrame, field: str, ticker: str) -> pd.Series:
    try:
        return df[(field, ticker)].dropna().astype(float)
    except Exception:
        return pd.Series(dtype=float)


def ret(s: pd.Series, n: int) -> float:
    if len(s) <= n:
        return np.nan
    return float(s.iloc[-1] / s.iloc[-1 - n] - 1)


def diff(s: pd.Series, n: int) -> float:
    if len(s) <= n:
        return np.nan
    return float(s.iloc[-1] - s.iloc[-1 - n])


def ma(s: pd.Series, n: int) -> float:
    if len(s) < n:
        return np.nan
    return float(s.tail(n).mean())


def rsi(s: pd.Series, n: int = 14) -> float:
    if len(s) < n + 2:
        return np.nan
    delta = s.diff().dropna()
    gain = delta.clip(lower=0).tail(n).mean()
    loss = -delta.clip(upper=0).tail(n).mean()
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def drawdown_from_high(s: pd.Series, n: int = 252) -> float:
    if len(s) < 2:
        return np.nan
    recent = s.tail(n)
    high = recent.max()
    if high <= 0:
        return np.nan
    return float(s.iloc[-1] / high - 1)


def volume_zscore(df: pd.DataFrame, ticker: str, window: int = 60) -> float:
    v = series(df, "Volume", ticker)
    if len(v) < window + 1:
        return np.nan
    base = v.tail(window)
    std = base.std()
    if std == 0 or pd.isna(std):
        return np.nan
    return float((v.iloc[-1] - base.mean()) / std)


def download_market_data() -> pd.DataFrame:
    data = yf.download(
        tickers=list(TICKERS.keys()),
        period="1y",
        interval="1d",
        group_by="column",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    return data


def add_signal(signals: List[str], score: int, title: str, reason: str) -> int:
    signals.append(f"+{score}｜{title}：{reason}")
    return score


@dataclass
class RadarResult:
    name: str
    score: int
    max_score: int
    signals: List[str]
    notes: List[str]

    @property
    def risk_pct(self) -> float:
        if self.max_score <= 0:
            return 0.0
        return min(100.0, self.score / self.max_score * 100)


# ------------------------------------------------------------
# 台股融資：TWSE / TPEX / 第三方備援資料
# ------------------------------------------------------------
def _numeric_cells(row) -> List[float]:
    vals = []
    for x in row:
        v = safe_float(x)
        if not pd.isna(v):
            vals.append(v)
    return vals


def _sum_today_balance_from_twse_table(table: dict) -> Optional[Dict[str, float]]:
    """解析 TWSE tables[n] 格式。

    TWSE 的 MI_MARGN 常見格式是 tables 裡面帶 fields/data，fields 中有兩組「今日餘額」：
    第一組是融資今日餘額，第二組是融券今日餘額。若查 ALL，data 是個股明細，直接合計即可。
    """
    fields = [str(x) for x in (table.get("fields") or [])]
    data = table.get("data") or []
    if not fields or not data:
        return None

    today_idx = [i for i, f in enumerate(fields) if "今日餘額" in f]
    if len(today_idx) < 1:
        return None

    margin_idx = today_idx[0]
    short_idx = today_idx[1] if len(today_idx) >= 2 else None

    # 若存在合計列，優先用合計列，避免個股與總計重複加總。
    total_rows = []
    normal_rows = []
    for row in data:
        if not isinstance(row, (list, tuple)):
            continue
        row_text = " ".join(str(x) for x in row)
        if "合計" in row_text or "總計" in row_text:
            total_rows.append(row)
        else:
            normal_rows.append(row)

    rows = total_rows if total_rows else normal_rows
    margin_sum = 0.0
    short_sum = 0.0
    margin_count = 0
    short_count = 0
    for row in rows:
        if len(row) > margin_idx:
            v = safe_float(row[margin_idx])
            if not pd.isna(v):
                margin_sum += v
                margin_count += 1
        if short_idx is not None and len(row) > short_idx:
            v = safe_float(row[short_idx])
            if not pd.isna(v):
                short_sum += v
                short_count += 1

    if margin_count <= 0 or margin_sum <= 0:
        return None
    out = {"margin_balance": margin_sum, "source": "TWSE tables/data"}
    if short_count > 0:
        out["short_balance"] = short_sum
    return out


def _parse_twse_margin_json(js: dict, date_str: str) -> Optional[Dict[str, float]]:
    if not isinstance(js, dict):
        return None

    # 新版常見：{'tables': [{...}, {'fields': [...], 'data': [...]}]}
    for table in js.get("tables") or []:
        if isinstance(table, dict):
            parsed = _sum_today_balance_from_twse_table(table)
            if parsed:
                parsed["date"] = date_str
                return parsed

    # 舊版或簡化版：data / aaData 直接放 row。
    rows = js.get("data") or js.get("aaData") or []
    result = {"date": date_str, "source": "TWSE data/aaData"}
    if rows:
        # 先找有「融資」「餘額」的列。
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            row_text = " ".join(str(x) for x in row)
            nums = _numeric_cells(row)
            if not nums:
                continue
            if "融資" in row_text and "餘額" in row_text:
                result["margin_balance"] = nums[-1]
            if "融券" in row_text and "餘額" in row_text:
                result["short_balance"] = nums[-1]
        if "margin_balance" in result:
            return result

        # 再退一步：若 fields 有今日餘額而 data 是個股明細，合計第一組今日餘額。
        parsed = _sum_today_balance_from_twse_table({"fields": js.get("fields") or [], "data": rows})
        if parsed:
            parsed["date"] = date_str
            return parsed

    return None


def twse_margin_one_day(day: datetime) -> Optional[Dict[str, float]]:
    """抓取單日上市融資融券餘額。

    V11 強化點：
    1) 支援 TWSE 新版 tables/data 結構，不再只看最外層 data。
    2) selectType 同時嘗試 ALL、MS 與常見市場統計代碼。
    3) 若官方回空，後面會再由第三方頁面做「顯示備援」，但評分仍以官方/可連續歷史為主。
    """
    date_str = day.strftime("%Y%m%d")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 GlobalMarketRadarV11",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.twse.com.tw/zh/trading/margin/mi-margn.html",
        "Connection": "keep-alive",
    }
    urls = [
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
        "https://www.twse.com.tw/exchangeReport/MI_MARGN",
    ]
    select_types = ["ALL", "MS", "EW", "24", "25", "13"]

    sess = requests.Session()
    try:
        sess.get("https://www.twse.com.tw/", headers=headers, timeout=10)
    except Exception:
        pass

    for url in urls:
        for select_type in select_types:
            try:
                params = {
                    "response": "json",
                    "date": date_str,
                    "selectType": select_type,
                    "_": str(int(time.time() * 1000)),
                }
                r = sess.get(url, params=params, headers=headers, timeout=15)
                if r.status_code != 200 or not r.text.strip():
                    continue
                js = r.json()
                parsed = _parse_twse_margin_json(js, date_str)
                if parsed and parsed.get("margin_balance", 0) > 0:
                    parsed["source"] = f"{parsed.get('source','TWSE')} / selectType={select_type}"
                    return parsed
            except Exception:
                continue
    return None



def fetch_twse_margin_official_page_latest() -> pd.DataFrame:
    """TWSE 官方頁面 HTML 備援。

    有時 json API 會被空日期、版面或結構變動影響，但官方網頁本身仍有
    「信用交易統計」摘要。這個函式只抓最新一筆，主要用來「顯示目前融資水位」；
    若只有一筆，不做趨勢扣分。
    """
    urls = [
        "https://www.twse.com.tw/zh/trading/margin/mi-margn.html",
        "https://www.twse.com.tw/en/trading/margin/mi-margn.html",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 GlobalMarketRadarV11",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.twse.com.tw/",
    }

    all_rows = []
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200 or not r.text:
                continue
            raw = r.text
            text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
            text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"&nbsp;|&#160;", " ", text)
            text = re.sub(r"\s+", " ", text)

            date_str = TODAY.strftime("%Y%m%d")
            mdate = re.search(r"(\d{3})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
            if mdate:
                y = int(mdate.group(1)) + 1911
                date_str = f"{y:04d}{int(mdate.group(2)):02d}{int(mdate.group(3)):02d}"
            else:
                mdate = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text)
                if mdate:
                    date_str = f"{int(mdate.group(1)):04d}{int(mdate.group(2)):02d}{int(mdate.group(3)):02d}"

            # Pattern A：Google / 摘要類文字：融資餘額為 5,930.47 億元
            patterns = [
                r"(?:集中市場|上市|台股)?融資餘額(?:為|：|:)?\s*([0-9,]+(?:\.[0-9]+)?)\s*億",
                r"Margin\s+Balance(?:\s*\(.*?\))?(?:：|:)?\s*([0-9,]+(?:\.[0-9]+)?)",
            ]
            for pat in patterns:
                m = re.search(pat, text, flags=re.I)
                if m:
                    val = safe_float(m.group(1))
                    if not pd.isna(val) and val > 0:
                        all_rows.append({
                            "date": date_str,
                            "margin_balance": val,
                            "source": "TWSE official page / latest displayed value",
                        })
                        break
            if all_rows:
                continue

            # Pattern B：官方信用交易統計表。抓「融資(交易單位)」列最後一個數字當今日餘額。
            m = re.search(r"融資\s*\(?交易單位\)?\s*((?:[0-9,]+\s*){2,8})", text)
            if m:
                nums = [safe_float(x) for x in re.findall(r"[0-9,]+", m.group(1))]
                nums = [x for x in nums if not pd.isna(x)]
                if nums:
                    all_rows.append({
                        "date": date_str,
                        "margin_balance": nums[-1],
                        "source": "TWSE official page / 融資交易單位今日餘額",
                    })
                    continue

            # Pattern C：英文官方頁。Margin Purchase (Trading unit), ... 今日餘額通常是最後一個數字。
            m = re.search(r"Margin\s+Purchase\s*\(Trading\s+unit\)\s*((?:[0-9,]+\s*){2,8})", text, flags=re.I)
            if m:
                nums = [safe_float(x) for x in re.findall(r"[0-9,]+", m.group(1))]
                nums = [x for x in nums if not pd.isna(x)]
                if nums:
                    all_rows.append({
                        "date": date_str,
                        "margin_balance": nums[-1],
                        "source": "TWSE official page / Margin Purchase trading-unit balance",
                    })
                    continue
        except Exception:
            continue

    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).drop_duplicates("date").sort_values("date")
    df["margin_balance"] = pd.to_numeric(df["margin_balance"], errors="coerce")
    df["source_group"] = "TWSE official page latest"
    return df


def _parse_third_party_margin_text(text: str, source: str) -> List[Dict[str, float]]:
    """從公開頁面的文字中抓取近期融資餘額。

    這是備援，不拿來硬當官方資料。若能抓到多日，就可做趨勢參考；若只抓到單日，只顯示不扣分。
    """
    if not text:
        return []
    text = re.sub(r"\s+", " ", text)
    rows = []

    # Pattern 1：2026/06/05 ... 融資餘額(億)5,666.49
    for m in re.finditer(r"(20\d{2}/\d{2}/\d{2}).{0,260}?融資餘額(?:\(億\))?\s*([0-9,]+(?:\.[0-9]+)?)", text):
        date = m.group(1).replace("/", "")
        val = safe_float(m.group(2))
        if not pd.isna(val):
            rows.append({"date": date, "margin_balance": val, "source": source + " / 億元"})

    # Pattern 2：Yahoo 類型：2026/06/05 -59.86 5,666.49
    if len(rows) < 2:
        for m in re.finditer(r"(20\d{2}/\d{2}/\d{2}).{0,80}?[-+]?\d+(?:\.\d+)?[^0-9]{1,20}([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+))", text):
            date = m.group(1).replace("/", "")
            val = safe_float(m.group(2))
            # 台股上市融資餘額近年通常是數千億；避免把指數或張數誤抓。
            if not pd.isna(val) and 1000 <= val <= 10000:
                rows.append({"date": date, "margin_balance": val, "source": source + " / inferred 億元"})

    # Pattern 3：單日：融資餘額5,666.49億元 / 融資餘額(億)5,666.49
    if not rows:
        m = re.search(r"融資餘額(?:\(億\))?\s*([0-9,]+(?:\.[0-9]+)?)\s*億?元?", text)
        if m:
            val = safe_float(m.group(1))
            if not pd.isna(val):
                rows.append({"date": TODAY.strftime("%Y%m%d"), "margin_balance": val, "source": source + " / latest 億元"})

    # 去重排序
    dedup = {}
    for r in rows:
        dedup[r["date"]] = r
    return [dedup[k] for k in sorted(dedup)]


def fetch_margin_third_party_history() -> pd.DataFrame:
    sources = [
        ("Yahoo資券餘額", "https://tw.stock.yahoo.com/margin-balance"),
        ("WantGoo資券進出", "https://www.wantgoo.com/stock/margin-trading/market-price/taiex"),
        ("KGI大盤動態", "https://www.kgi.com.tw/zh-tw/product-market/stock-market-overview/tw-stock-market/tw-stock-market-detail?a=B658010E71E243C4A1D6B5F7BE914BDC&b=5D48401A7CE148CD8ABAC965F9B5AFBF"),
        ("PSC信用交易", "https://www.pscnet.com.tw/pscnetStock/menuMain.do?main_id=386032846c000000ccd145898ac293b6"),
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 GlobalMarketRadarV11",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    all_rows = []
    for name, url in sources:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            rows = _parse_third_party_margin_text(r.text, name)
            if rows:
                all_rows.extend(rows)
                if len(rows) >= 5:
                    break
        except Exception:
            continue
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).drop_duplicates("date").sort_values("date")
    df["margin_balance"] = pd.to_numeric(df["margin_balance"], errors="coerce")
    return df





# ------------------------------------------------------------
# TPEx 櫃買融資：輔助觀察，不混入 TWSE 集中市場主數列
# ------------------------------------------------------------
def _decode_response_text(resp: requests.Response) -> str:
    for enc in ["utf-8-sig", "utf-8", "big5", "cp950"]:
        try:
            return resp.content.decode(enc)
        except Exception:
            continue
    return resp.text or ""


def _parse_tpex_historical_text(text: str, source: str) -> List[Dict[str, object]]:
    """Parse TPEx/third-party OTC margin balance in 億元 when available.

    The TPEx official per-stock table is often in shares/trading units. For trend comparability,
    this parser only accepts rows that look like market-level NT$100m balance series.
    """
    if not text:
        return []
    text = re.sub(r"\s+", " ", text)
    rows: List[Dict[str, object]] = []

    # HiStock style: 06/18 2,054.7 17.1 40,982 ...
    for m in re.finditer(r"(\d{2}/\d{2})\s+([0-9,]+(?:\.[0-9]+)?)\s+[-+]?[0-9,]+(?:\.[0-9]+)?\s+[0-9,]+", text):
        date = f"{TODAY.year}{m.group(1).replace('/', '')}"
        val = safe_float(m.group(2))
        if not pd.isna(val) and 500 <= val <= 5000:
            rows.append({
                "date": date,
                "margin_balance": val,
                "short_balance": np.nan,
                "source": source + " / TPEx inferred 億元",
                "source_group": "TPEx auxiliary",
            })

    # Generic date style: 2026/06/18 ... 融資餘額(億)2,054.7
    for m in re.finditer(r"(20\d{2}/\d{2}/\d{2}).{0,220}?(?:上櫃|櫃買|OTC).{0,80}?融資餘額(?:\(億\))?\s*([0-9,]+(?:\.[0-9]+)?)", text):
        val = safe_float(m.group(2))
        if not pd.isna(val) and 500 <= val <= 5000:
            rows.append({
                "date": m.group(1).replace("/", ""),
                "margin_balance": val,
                "short_balance": np.nan,
                "source": source + " / TPEx latest 億元",
                "source_group": "TPEx auxiliary",
            })

    # Dedup by date, keep latest source encountered.
    dedup: Dict[str, Dict[str, object]] = {}
    for r in rows:
        dedup[str(r["date"])] = r
    return [dedup[k] for k in sorted(dedup)]


def _parse_tpex_official_csv_or_html(text: str, source: str) -> List[Dict[str, object]]:
    """Parse TPEx official per-stock margin table.

    This uses official summed '資餘額' trading-unit/share-like values as a backup only.
    It is stored separately from TWSE and labelled as TPEx official trading-unit.
    """
    if not text:
        return []
    from io import StringIO

    # Try CSV/table parse first.
    candidates = []
    try:
        dfs = pd.read_html(StringIO(text))
        candidates.extend(dfs)
    except Exception:
        pass
    try:
        df_csv = pd.read_csv(StringIO(text))
        candidates.append(df_csv)
    except Exception:
        pass

    date_str = TODAY.strftime("%Y%m%d")
    mdate = re.search(r"(\d{3})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if mdate:
        date_str = f"{int(mdate.group(1)) + 1911:04d}{int(mdate.group(2)):02d}{int(mdate.group(3)):02d}"
    else:
        mdate = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text)
        if mdate:
            date_str = f"{int(mdate.group(1)):04d}{int(mdate.group(2)):02d}{int(mdate.group(3)):02d}"

    for df0 in candidates:
        try:
            df = df0.copy()
            df.columns = [str(c) for c in df.columns]
            margin_col = None
            short_col = None
            for c in df.columns:
                cstr = str(c)
                if ("資餘額" in cstr or "資餘額(張)" in cstr or "資餘額" in cstr.replace(" ", "")):
                    margin_col = c
                if ("券餘額" in cstr or "券餘額(張)" in cstr or "券餘額" in cstr.replace(" ", "")):
                    short_col = c
            if margin_col is None:
                # Some exports have columns split/renamed. Try positional fallback after stock code/name.
                joined = " ".join(df.columns)
                if "前資" in joined and "資買" in joined and "資賣" in joined and len(df.columns) >= 8:
                    margin_col = df.columns[7]
                    if len(df.columns) >= 16:
                        short_col = df.columns[15]
            if margin_col is None:
                continue
            margin_vals = pd.to_numeric(df[margin_col].astype(str).str.replace(",", "", regex=False), errors="coerce")
            margin_sum = float(margin_vals.dropna().sum())
            short_sum = np.nan
            if short_col is not None:
                short_vals = pd.to_numeric(df[short_col].astype(str).str.replace(",", "", regex=False), errors="coerce")
                if short_vals.dropna().shape[0] > 0:
                    short_sum = float(short_vals.dropna().sum())
            if margin_sum > 0:
                return [{
                    "date": date_str,
                    "margin_balance": margin_sum,
                    "short_balance": short_sum,
                    "source": source + " / TPEx official summed trading-unit",
                    "source_group": "TPEx official auxiliary",
                }]
        except Exception:
            continue
    return []


def fetch_tpex_margin_live() -> pd.DataFrame:
    """Fetch TPEx OTC margin as auxiliary series.

    Priority:
    1) HiStock/third-party market-level 億元 series, best for short trend.
    2) TPEx official open-data/table summed trading-unit, display-only backup.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 GlobalMarketRadarV12",
        "Accept": "text/html,text/csv,application/json,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.tpex.org.tw/",
    }

    # First: market-level 億元 history from pages that expose OTC aggregate rows.
    historical_sources = [
        ("HiStock櫃買資券", "https://histock.tw/stock/three.aspx?m=mg&no=TWOI"),
        ("Yahoo資券餘額", "https://tw.stock.yahoo.com/margin-balance"),
        ("WantGoo資券進出", "https://www.wantgoo.com/stock/margin-trading/market-price/tpex"),
    ]
    all_rows: List[Dict[str, object]] = []
    for name, url in historical_sources:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200 or not r.text:
                continue
            rows = _parse_tpex_historical_text(r.text, name)
            if rows:
                all_rows.extend(rows)
                if len(rows) >= 5:
                    break
        except Exception:
            continue
    if all_rows:
        df = pd.DataFrame(all_rows)
        df["date"] = df["date"].astype(str).str.replace("-", "", regex=False)
        df["margin_balance"] = pd.to_numeric(df["margin_balance"], errors="coerce")
        df["short_balance"] = pd.to_numeric(df.get("short_balance", np.nan), errors="coerce")
        df = df.dropna(subset=["margin_balance"]).drop_duplicates("date", keep="last").sort_values("date")
        return df[["date", "margin_balance", "short_balance", "source", "source_group"]]

    # Second: official TPEx current per-stock table / open data, summed.
    official_sources = [
        ("TPEx open data CSV", "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=data"),
        ("TPEx official page", "https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html"),
    ]
    for name, url in official_sources:
        try:
            r = requests.get(url, headers=headers, timeout=18)
            if r.status_code != 200 or not r.content:
                continue
            text = _decode_response_text(r)
            rows = _parse_tpex_official_csv_or_html(text, name)
            if rows:
                df = pd.DataFrame(rows)
                df["date"] = df["date"].astype(str).str.replace("-", "", regex=False)
                df["margin_balance"] = pd.to_numeric(df["margin_balance"], errors="coerce")
                df["short_balance"] = pd.to_numeric(df.get("short_balance", np.nan), errors="coerce")
                return df[["date", "margin_balance", "short_balance", "source", "source_group"]]
        except Exception:
            continue
    return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])


def load_tpex_margin_history(path: str = TPEX_MARGIN_HISTORY_FILE) -> pd.DataFrame:
    try:
        if not os.path.exists(path):
            return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
        df = pd.read_csv(path, dtype={"date": str})
        if "date" not in df.columns or "margin_balance" not in df.columns:
            return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
        df["date"] = df["date"].astype(str).str.replace("-", "", regex=False)
        df["margin_balance"] = pd.to_numeric(df["margin_balance"], errors="coerce")
        if "short_balance" not in df.columns:
            df["short_balance"] = np.nan
        df["short_balance"] = pd.to_numeric(df["short_balance"], errors="coerce")
        for c in ["source", "source_group"]:
            if c not in df.columns:
                df[c] = ""
            df[c] = df[c].fillna("").astype(str)
        df = df.dropna(subset=["margin_balance"]).drop_duplicates("date", keep="last").sort_values("date")
        return df[["date", "margin_balance", "short_balance", "source", "source_group"]]
    except Exception:
        return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])


def save_tpex_margin_history(df: pd.DataFrame, path: str = TPEX_MARGIN_HISTORY_FILE) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if df is None or df.empty:
            return
        out = df.copy()
        out["date"] = out["date"].astype(str).str.replace("-", "", regex=False)
        out["margin_balance"] = pd.to_numeric(out["margin_balance"], errors="coerce")
        if "short_balance" not in out.columns:
            out["short_balance"] = np.nan
        out["short_balance"] = pd.to_numeric(out["short_balance"], errors="coerce")
        for c in ["source", "source_group"]:
            if c not in out.columns:
                out[c] = ""
        out = out.dropna(subset=["margin_balance"]).drop_duplicates("date", keep="last").sort_values("date").tail(260)
        out[["date", "margin_balance", "short_balance", "source", "source_group"]].to_csv(path, index=False, encoding="utf-8-sig")
    except Exception as e:
        print("save_tpex_margin_history error:", e)


def fetch_tpex_margin_history() -> pd.DataFrame:
    local = load_tpex_margin_history()
    live = fetch_tpex_margin_live()
    frames = []
    if local is not None and not local.empty:
        frames.append(local)
    if live is not None and not live.empty:
        frames.append(live)
    if not frames:
        return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged["date"] = merged["date"].astype(str).str.replace("-", "", regex=False)
    merged["margin_balance"] = pd.to_numeric(merged["margin_balance"], errors="coerce")
    if "short_balance" not in merged.columns:
        merged["short_balance"] = np.nan
    merged["short_balance"] = pd.to_numeric(merged["short_balance"], errors="coerce")
    for c in ["source", "source_group"]:
        if c not in merged.columns:
            merged[c] = ""
    merged = merged.dropna(subset=["margin_balance"]).drop_duplicates("date", keep="last").sort_values("date").tail(260)
    save_tpex_margin_history(merged)
    return merged[["date", "margin_balance", "short_balance", "source", "source_group"]]


def load_local_margin_history(path: str = MARGIN_HISTORY_FILE) -> pd.DataFrame:
    """Load accumulated Taiwan margin history from repo file."""
    try:
        if not os.path.exists(path):
            return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
        df = pd.read_csv(path, dtype={"date": str})
        if "date" not in df.columns or "margin_balance" not in df.columns:
            return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
        df["date"] = df["date"].astype(str).str.replace("-", "", regex=False)
        df["margin_balance"] = pd.to_numeric(df["margin_balance"], errors="coerce")
        if "short_balance" in df.columns:
            df["short_balance"] = pd.to_numeric(df["short_balance"], errors="coerce")
        else:
            df["short_balance"] = np.nan
        for c in ["source", "source_group"]:
            if c not in df.columns:
                df[c] = ""
        df = clean_margin_history_by_priority(df)
        return df[["date", "margin_balance", "short_balance", "source", "source_group"]]
    except Exception:
        return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])


def save_local_margin_history(df: pd.DataFrame, path: str = MARGIN_HISTORY_FILE) -> None:
    """Persist accumulated Taiwan margin history to repo file."""
    try:
        if df is None or df.empty:
            return
        out = df.copy()
        out["date"] = out["date"].astype(str).str.replace("-", "", regex=False)
        out["margin_balance"] = pd.to_numeric(out["margin_balance"], errors="coerce")
        if "short_balance" not in out.columns:
            out["short_balance"] = np.nan
        out["short_balance"] = pd.to_numeric(out["short_balance"], errors="coerce")
        for c in ["source", "source_group"]:
            if c not in out.columns:
                out[c] = ""
        out = clean_margin_history_by_priority(out)
        # Keep last 260 records, enough for a year of trading days.
        out = out.tail(260)
        out[["date", "margin_balance", "short_balance", "source", "source_group"]].to_csv(path, index=False, encoding="utf-8-sig")
    except Exception as e:
        print("save_local_margin_history error:", e)



def margin_source_priority(row: pd.Series) -> int:
    """Higher is better. Prevent partial market data from overwriting ALL-market data."""
    src = str(row.get("source", "") or "")
    src_group = str(row.get("source_group", "") or "")
    mb = pd.to_numeric(row.get("margin_balance", np.nan), errors="coerce")

    if "selectType=ALL" in src:
        return 100
    if "TWSE official page" in src_group or "official page" in src:
        return 90
    if "manual" in src_group or "manual" in src:
        return 80
    if "third-party" in src_group or "Yahoo" in src or "WantGoo" in src:
        return 70
    if "selectType=24" in src:
        return 10
    if "TWSE" in src_group or "TWSE" in src:
        if not pd.isna(mb) and mb < 100_000_000:
            return 15
        return 60
    return 30


def clean_margin_history_by_priority(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate by date using source priority, not last-write-wins."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
    out = df.copy()
    out["date"] = out["date"].astype(str).str.replace("-", "", regex=False)
    out["margin_balance"] = pd.to_numeric(out["margin_balance"], errors="coerce")
    if "short_balance" not in out.columns:
        out["short_balance"] = np.nan
    out["short_balance"] = pd.to_numeric(out["short_balance"], errors="coerce")
    for c in ["source", "source_group"]:
        if c not in out.columns:
            out[c] = ""
        out[c] = out[c].fillna("").astype(str)

    out = out.dropna(subset=["margin_balance"])
    out["_priority"] = out.apply(margin_source_priority, axis=1)
    out = out.sort_values(["date", "_priority", "margin_balance"], ascending=[True, True, True])
    out = out.drop_duplicates("date", keep="last").sort_values("date")
    out = out.drop(columns=["_priority"], errors="ignore")
    return out[["date", "margin_balance", "short_balance", "source", "source_group"]]


def merge_margin_history(local_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    """Merge local and newly fetched margin data without allowing lower-quality rows to overwrite better rows."""
    frames = []
    if local_df is not None and not local_df.empty:
        frames.append(local_df)
    if new_df is not None and not new_df.empty:
        frames.append(new_df)
    if not frames:
        return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])
    df = pd.concat(frames, ignore_index=True, sort=False)
    return clean_margin_history_by_priority(df)

def fetch_twse_margin_live_batch(days_back: int = 28) -> pd.DataFrame:
    """Fetch latest available margin data from live sources only."""
    rows = []
    for i in range(days_back):
        item = twse_margin_one_day(TODAY - timedelta(days=i))
        if item:
            rows.append(item)
        time.sleep(0.18)
    if rows:
        df = pd.DataFrame(rows).sort_values("date")
        for c in ["margin_balance", "short_balance"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["source_group"] = "TWSE official"
        df = clean_margin_history_by_priority(df)
        return df

    # 官方 json 不足時，先抓 TWSE 官方網頁最新摘要；通常能顯示最新水位。
    try:
        official_latest = fetch_twse_margin_official_page_latest()
        if not official_latest.empty:
            return official_latest
    except Exception:
        pass

    # 官方頁也不足時，啟用第三方備援。
    try:
        backup = fetch_margin_third_party_history()
        if not backup.empty:
            backup["source_group"] = "third-party backup"
            return backup
    except Exception:
        pass

    return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])


def fetch_twse_margin_history(days_back: int = 28) -> pd.DataFrame:
    """Fetch Taiwan margin data with persistent local history.

    Stable long-term method:
    - Fetch whatever is available today from TWSE / official page / third-party.
    - Merge it into tw_margin_history.csv.
    - Use accumulated local history for 1-day / 5-day trend scoring.
    """
    local = load_local_margin_history()
    live = fetch_twse_margin_live_batch(days_back=days_back)
    merged = merge_margin_history(local, live)

    if not merged.empty:
        # Mark whether this run used accumulated local history.
        if len(local) > 0:
            merged["source_group"] = merged["source_group"].fillna("")
            # Keep individual source rows, but add a readable hint to the latest row.
            merged.loc[merged.index[-1], "source_group"] = (str(merged.iloc[-1].get("source_group", "")) + " + local history").strip(" +")
        save_local_margin_history(merged)
        return merged

    return pd.DataFrame(columns=["date", "margin_balance", "short_balance", "source", "source_group"])


# ------------------------------------------------------------
# 各模組評分
# ------------------------------------------------------------
def score_macro_pressure(df: pd.DataFrame) -> RadarResult:
    signals, notes = [], []
    score, max_score = 0, 120

    vix = series(df, "Close", "^VIX")
    move = series(df, "Close", "^MOVE")
    tnx = series(df, "Close", "^TNX") / 10.0
    tyx = series(df, "Close", "^TYX") / 10.0
    usdjpy = series(df, "Close", "JPY=X")
    gld = series(df, "Close", "GLD")
    uup = series(df, "Close", "UUP")
    oil = series(df, "Close", "CL=F")
    tlt = series(df, "Close", "TLT")

    if len(vix):
        v = vix.iloc[-1]
        if v >= 35:
            score += add_signal(signals, 24, "VIX 恐慌", f"VIX={num(v)}，波動已經不是打噴嚏，是發燒")
        elif v >= 30:
            score += add_signal(signals, 20, "VIX 高壓", f"VIX={num(v)}，市場進入明顯恐慌區")
        elif v >= 25:
            score += add_signal(signals, 14, "VIX 升溫", f"VIX={num(v)}，風險偏好明顯降溫")
        elif v >= 20:
            score += add_signal(signals, 8, "VIX 警戒", f"VIX={num(v)}，波動開始變大")
        r5 = ret(vix, 5)
        if not pd.isna(r5) and r5 > 0.25:
            score += add_signal(signals, 8, "波動急升", f"VIX 5日變化={pct(r5)}")
    else:
        notes.append("VIX 資料缺失")

    if len(move):
        mv = move.iloc[-1]
        mv20 = ret(move, 20)
        if mv >= 150:
            score += add_signal(signals, 18, "MOVE 債券波動高壓", f"MOVE={num(mv)}，美債市場壓力很高")
        elif mv >= 130:
            score += add_signal(signals, 12, "MOVE 升溫", f"MOVE={num(mv)}，債市波動升高")
        elif mv >= 115:
            score += add_signal(signals, 6, "MOVE 觀察", f"MOVE={num(mv)}，債券壓力需要留意")
        if not pd.isna(mv20) and mv20 > 0.15:
            score += add_signal(signals, 6, "債券波動升速快", f"MOVE 20日={pct(mv20)}")
    else:
        notes.append("MOVE 指數資料缺失；Yahoo Finance 有時不穩，這次不納入扣分")

    if len(tnx):
        y = tnx.iloc[-1]
        y5 = diff(tnx, 5)
        if y >= 5.0:
            score += add_signal(signals, 12, "美債殖利率高壓", f"10Y={num(y)}%")
        elif y >= 4.6:
            score += add_signal(signals, 7, "美債殖利率偏高", f"10Y={num(y)}%")
        if not pd.isna(y5) and y5 > 0.25:
            score += add_signal(signals, 8, "利率急升", f"10Y 5日上升 {bp(y5)}")

    if len(tyx) and tyx.iloc[-1] >= 5.0:
        score += add_signal(signals, 6, "長債壓力", f"30Y={num(tyx.iloc[-1])}%")

    if len(usdjpy):
        j = usdjpy.iloc[-1]
        r1 = ret(usdjpy, 1)
        r5 = ret(usdjpy, 5)
        r20 = ret(usdjpy, 20)
        # V12：日圓破底 / 干預敏感區分層。日圓單獨弱是宏觀壓力；若搭配 VIX / 信用變壞，才視為擴散風險。
        if j >= 161.95:
            score += add_signal(signals, 12, "日圓破關警戒", f"USDJPY={num(j)}，接近/突破 161.95，干預與套息交易 unwind 風險升高")
        elif j >= 161.5:
            score += add_signal(signals, 10, "日圓干預敏感區", f"USDJPY={num(j)}，接近 1986 年低位與日本干預敘事區")
        elif j >= 160:
            score += add_signal(signals, 9, "日圓極弱", f"USDJPY={num(j)}，日本干預/賣債敘事升溫")
        elif j >= 155:
            score += add_signal(signals, 5, "日圓偏弱", f"USDJPY={num(j)}")
        if not pd.isna(r1) and r1 > 0.015:
            score += add_signal(signals, 4, "日圓單日急貶", f"USDJPY 單日={pct(r1)}")
        if not pd.isna(r5) and r5 > 0.015:
            score += add_signal(signals, 6, "日圓5日急貶", f"USDJPY 5日={pct(r5)}")
        if not pd.isna(r20) and r20 > 0.03:
            score += add_signal(signals, 6, "日圓20日貶值加速", f"USDJPY 20日={pct(r20)}")
        if j >= 161.5 and len(vix) and vix.iloc[-1] >= 20:
            score += add_signal(signals, 5, "日圓破底 + VIX升溫", f"USDJPY={num(j)}，VIX={num(vix.iloc[-1])}，壓力開始擴散")
        try:
            hyg = series(df, "Close", "HYG")
            lqd = series(df, "Close", "LQD")
            ratio = (hyg / lqd).dropna()
            if j >= 161.5 and len(ratio) >= 60 and ratio.iloc[-1] < ma(ratio, 60):
                score += add_signal(signals, 5, "日圓破底 + 信用轉弱", f"USDJPY={num(j)}，HYG/LQD 低於60日線")
        except Exception:
            pass

    if len(oil):
        o = oil.iloc[-1]
        if o >= 100:
            score += add_signal(signals, 7, "油價高壓", f"WTI={num(o)}")
        elif o >= 90:
            score += add_signal(signals, 4, "油價偏高", f"WTI={num(o)}")

    if len(gld) and len(tlt):
        g20, t20 = ret(gld, 20), ret(tlt, 20)
        if not pd.isna(g20) and not pd.isna(t20) and g20 > 0.04 and t20 < -0.04:
            score += add_signal(signals, 7, "金強債弱", f"GLD 20日={pct(g20)}，TLT 20日={pct(t20)}，市場在找避險但不愛長債")

    if len(uup):
        u20 = ret(uup, 20)
        if not pd.isna(u20) and u20 > 0.04:
            score += add_signal(signals, 4, "美元走強", f"UUP 20日={pct(u20)}，流動性壓力可能上升")

    return RadarResult("全球壓力宏觀雷達", min(score, max_score), max_score, signals, notes)



# ------------------------------------------------------------
# V12：Fed 流動性雷達（FRED 硬數據）
# ------------------------------------------------------------
def fetch_fred_series(series_id: str) -> pd.Series:
    """Fetch FRED public CSV without API key, with retry and parser fallback."""
    urls = [
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?cosd=2018-01-01&id={series_id}",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 GlobalMarketRadarV12",
        "Accept": "text/csv,text/plain,*/*",
        "Cache-Control": "no-cache",
    }
    from io import StringIO
    for url in urls:
        for attempt in range(2):
            try:
                r = requests.get(url, timeout=25, headers=headers)
                if r.status_code != 200 or not r.text.strip():
                    time.sleep(0.8)
                    continue
                df = pd.read_csv(StringIO(r.text))
                if df.empty or "DATE" not in df.columns or series_id not in df.columns:
                    time.sleep(0.8)
                    continue
                df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
                s = pd.to_numeric(df[series_id].replace(".", np.nan), errors="coerce")
                out = pd.Series(s.values, index=df["DATE"]).dropna().astype(float)
                if len(out) > 0:
                    return out
            except Exception:
                time.sleep(0.8)
                continue

    # Last fallback: pandas direct reader. Sometimes requests succeeds poorly behind GitHub Actions.
    for url in urls:
        try:
            df = pd.read_csv(url)
            if not df.empty and "DATE" in df.columns and series_id in df.columns:
                df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
                s = pd.to_numeric(df[series_id].replace(".", np.nan), errors="coerce")
                out = pd.Series(s.values, index=df["DATE"]).dropna().astype(float)
                if len(out) > 0:
                    return out
        except Exception:
            continue
    return pd.Series(dtype=float)


def score_fed_liquidity(df: pd.DataFrame) -> RadarResult:
    signals, notes = [], []
    score, max_score = 0, 80

    # Units:
    # WALCL: Millions of dollars, weekly
    # WRESBAL: Billions of dollars, weekly
    # RRPONTSYD: Billions of dollars, daily
    # DPCREDIT: Billions of dollars, weekly
    walcl = fetch_fred_series("WALCL")       # Fed balance sheet
    reserves = fetch_fred_series("WRESBAL")  # reserve balances
    rrp = fetch_fred_series("RRPONTSYD")     # overnight reverse repo
    discount = fetch_fred_series("DPCREDIT") # discount window primary credit

    if len(walcl) >= 5:
        chg4 = walcl.iloc[-1] - walcl.iloc[-5]  # million USD, roughly 4 weeks
        notes.append(f"Fed資產負債表 WALCL：{walcl.index[-1].date()}｜4週變化約 {chg4/1000:.1f} 十億美元")
        if chg4 > 250_000:
            score += add_signal(signals, 12, "Fed資產負債表重新擴張", f"4週增加約 {chg4/1000:.1f} 十億美元")
        elif chg4 > 100_000:
            score += add_signal(signals, 8, "Fed資產負債表擴張", f"4週增加約 {chg4/1000:.1f} 十億美元")
    else:
        notes.append("Fed資產負債表 WALCL 資料不足（可用 FRED_API_KEY 改善）；FRED 可能暫時無法讀取。")

    if len(reserves) >= 5:
        chg4 = reserves.iloc[-1] - reserves.iloc[-5]  # billion USD
        notes.append(f"銀行準備金 WRESBAL：{reserves.index[-1].date()}｜4週變化約 {chg4:.1f} 十億美元")
        if chg4 > 200:
            score += add_signal(signals, 12, "銀行準備金大增", f"4週增加約 {chg4:.1f} 十億美元，流動性重新外溢")
        elif chg4 > 100:
            score += add_signal(signals, 8, "銀行準備金增加", f"4週增加約 {chg4:.1f} 十億美元")
    else:
        notes.append("銀行準備金 WRESBAL 資料不足（可用 FRED_API_KEY 改善）。")

    if len(rrp) >= 20:
        chg20 = rrp.iloc[-1] - rrp.iloc[-20]
        notes.append(f"逆回購 RRP：{rrp.index[-1].date()}｜20筆變化約 {chg20:.1f} 十億美元")
        if chg20 < -200:
            score += add_signal(signals, 8, "RRP快速下降", f"20筆減少約 {abs(chg20):.1f} 十億美元，市場流動性可能被釋放")
        elif chg20 < -100:
            score += add_signal(signals, 5, "RRP下降", f"20筆減少約 {abs(chg20):.1f} 十億美元")
    else:
        notes.append("逆回購 RRP 資料不足（可用 FRED_API_KEY 改善）。")

    if len(discount) >= 5:
        chg4 = discount.iloc[-1] - discount.iloc[-5]
        level = discount.iloc[-1]
        notes.append(f"貼現窗口 DPCREDIT：{discount.index[-1].date()}｜水位約 {level:.1f} 十億美元｜4週變化 {chg4:.1f}")
        if level > 50 or chg4 > 20:
            score += add_signal(signals, 14, "貼現窗口壓力升高", f"DPCREDIT={level:.1f} 十億美元，4週變化 {chg4:.1f}")
        elif level > 10 or chg4 > 5:
            score += add_signal(signals, 7, "貼現窗口使用升溫", f"DPCREDIT={level:.1f} 十億美元，4週變化 {chg4:.1f}")
    else:
        notes.append("貼現窗口 DPCREDIT 資料不足（可用 FRED_API_KEY 改善）。")

    # High rates + renewed liquidity = late-cycle pressure / policy contradiction watch
    try:
        tnx = series(df, "Close", "^TNX") / 10.0
        if len(tnx) and tnx.iloc[-1] >= 4.5 and any("資產負債表" in x or "準備金" in x for x in signals):
            score += add_signal(signals, 6, "高利率下流動性放鬆", f"10Y={num(tnx.iloc[-1])}%；高利率 + Fed流動性擴張 = 金融系統壓力升級觀察")
    except Exception:
        pass

    if not signals:
        notes.append("無明顯Fed流動性警訊；此模組看硬數據，不看新聞口號。")
    return RadarResult("Fed流動性雷達", min(score, max_score), max_score, signals, notes)


def score_credit(df: pd.DataFrame) -> RadarResult:
    signals, notes = [], []
    score, max_score = 0, 80

    hyg = series(df, "Close", "HYG")
    jnk = series(df, "Close", "JNK")
    lqd = series(df, "Close", "LQD")

    if len(hyg) and len(lqd):
        ratio = (hyg / lqd).dropna()
        if len(ratio) >= 60:
            r60 = ma(ratio, 60)
            r200 = ma(ratio, 200) if len(ratio) >= 200 else np.nan
            r1, r5, r20 = ret(ratio, 1), ret(ratio, 5), ret(ratio, 20)
            trend60 = "高於60日線" if not pd.isna(r60) and ratio.iloc[-1] >= r60 else "低於60日線"
            trend200 = "；高於200日線" if not pd.isna(r200) and ratio.iloc[-1] >= r200 else ("；低於200日線" if not pd.isna(r200) else "；200日資料不足")
            notes.append(f"HYG/LQD={num(ratio.iloc[-1],4)}；1日 {pct(r1)}；5日 {pct(r5)}；20日 {pct(r20)}；{trend60}{trend200}")
            if ratio.iloc[-1] < r60:
                score += add_signal(signals, 12, "信用利差 proxy 轉弱", "HYG/LQD 跌破60日均線，垃圾債相對投資級債變弱")
            if not pd.isna(r200) and ratio.iloc[-1] < r200:
                score += add_signal(signals, 16, "信用利差 proxy 長線轉弱", "HYG/LQD 跌破200日均線，這比股價回檔更值得怕")
            if not pd.isna(r1) and r1 < -0.012:
                score += add_signal(signals, 7, "信用單日急縮", f"HYG/LQD 單日={pct(r1)}")
            if not pd.isna(r5) and r5 < -0.02:
                score += add_signal(signals, 8, "信用一週轉弱", f"HYG/LQD 5日={pct(r5)}")
            if not pd.isna(r20) and r20 < -0.025:
                score += add_signal(signals, 12, "信用快速收縮", f"HYG/LQD 20日={pct(r20)}")
        else:
            notes.append(f"HYG/LQD 歷史資料不足，目前只有 {len(ratio)} 筆")
    else:
        notes.append("HYG 或 LQD 資料缺失")

    if len(jnk) and len(lqd):
        ratio2 = (jnk / lqd).dropna()
        if len(ratio2) >= 20:
            r20 = ret(ratio2, 20)
            notes.append(f"JNK/LQD={num(ratio2.iloc[-1],4)}；20日 {pct(r20)}")
            if not pd.isna(r20) and r20 < -0.025:
                score += add_signal(signals, 8, "JNK/LQD 同步轉弱", f"JNK/LQD 20日={pct(r20)}")

    return RadarResult("信用利差 proxy 雷達", min(score, max_score), max_score, signals, notes)


def score_market(df: pd.DataFrame) -> RadarResult:
    signals, notes = [], []
    score, max_score = 0, 110

    watch = ["SPY", "QQQ", "SMH", "SOXX", "IWM", "EWT", "EWJ", "00662.TW", "00670L.TW", "^TWII"]
    weak_count = 0
    strong_volume_down = 0

    for t in watch:
        s = series(df, "Close", t)
        if len(s) < 60:
            notes.append(f"{t} 資料不足")
            continue
        c, m60, m200 = s.iloc[-1], ma(s, 60), ma(s, 200)
        r20v, dd = ret(s, 20), drawdown_from_high(s, 252)
        vz = volume_zscore(df, t, 60)

        if not pd.isna(m200) and c < m200:
            weak_count += 1
        if not pd.isna(dd) and dd < -0.12:
            weak_count += 1
        if not pd.isna(vz) and vz > 1.8 and not pd.isna(r20v) and r20v < -0.04:
            strong_volume_down += 1

        if t in ["QQQ", "SMH", "SOXX", "00662.TW", "00670L.TW"]:
            if not pd.isna(m60) and c < m60:
                score += add_signal(signals, 5, f"{t} 跌破60日", "科技/半導體動能降溫")
            if not pd.isna(dd) and dd < -0.15:
                score += add_signal(signals, 6, f"{t} 回撤擴大", f"距一年高點={pct(dd)}")

    if weak_count >= 8:
        score += add_signal(signals, 22, "全球股市廣泛轉弱", f"主要觀察標的弱勢計數={weak_count}")
    elif weak_count >= 4:
        score += add_signal(signals, 13, "股市弱勢擴散", f"主要觀察標的弱勢計數={weak_count}")

    if strong_volume_down >= 3:
        score += add_signal(signals, 10, "放量下跌擴散", f"至少 {strong_volume_down} 個標的出現放量下跌")

    notes.insert(0, f"弱勢計數={weak_count}；放量下跌計數={strong_volume_down}。0分代表沒有觸發『轉弱/崩壞』條件，不代表沒讀到資料。")
    for t in ["QQQ", "SMH", "00662.TW", "00670L.TW", "^TWII"]:
        s = series(df, "Close", t)
        if len(s) >= 20:
            notes.append(f"{t}：最新 {num(s.iloc[-1])}；20日 {pct(ret(s,20))}；距一年高點 {pct(drawdown_from_high(s,252))}")

    return RadarResult("全球市場動能雷達", min(score, max_score), max_score, signals, notes)


def score_breadth_greed(df: pd.DataFrame) -> RadarResult:
    """市場廣度與過熱 proxy。這不是 CNN Fear & Greed，而是可自動化的替代儀表。"""
    signals, notes = [], []
    score, max_score = 0, 80

    spy = series(df, "Close", "SPY")
    rsp = series(df, "Close", "RSP")
    iwm = series(df, "Close", "IWM")
    qqq = series(df, "Close", "QQQ")
    t00662 = series(df, "Close", "00662.TW")
    t00670 = series(df, "Close", "00670L.TW")

    if len(spy) >= 200:
        spy_rsi = rsi(spy)
        dist200 = spy.iloc[-1] / ma(spy, 200) - 1 if ma(spy, 200) else np.nan
        if not pd.isna(spy_rsi) and spy_rsi > 75:
            score += add_signal(signals, 8, "S&P 500 過熱", f"RSI={num(spy_rsi)}")
        if not pd.isna(dist200) and dist200 > 0.16:
            score += add_signal(signals, 8, "SPY 偏離長期均線", f"高於200日均線 {pct(dist200)}")

    if len(qqq) >= 200 and len(rsp) >= 200:
        qr = (qqq / rsp).dropna()
        if len(qr) >= 200:
            qr20 = ret(qr, 20)
            if not pd.isna(qr20) and qr20 > 0.06:
                score += add_signal(signals, 10, "科技股相對等權重過熱", f"QQQ/RSP 20日={pct(qr20)}，資金太集中")

    if len(rsp) >= 200 and len(spy) >= 200:
        breadth = (rsp / spy).dropna()
        b20 = ret(breadth, 20)
        b60 = ret(breadth, 60)
        if not pd.isna(b20) and b20 < -0.03:
            score += add_signal(signals, 10, "美股廣度轉弱", f"RSP/SPY 20日={pct(b20)}，權值股撐盤、廣度變差")
        if not pd.isna(b60) and b60 < -0.06:
            score += add_signal(signals, 8, "美股廣度中期惡化", f"RSP/SPY 60日={pct(b60)}")

    if len(iwm) >= 200 and len(spy) >= 200:
        small = (iwm / spy).dropna()
        s20 = ret(small, 20)
        if not pd.isna(s20) and s20 < -0.04:
            score += add_signal(signals, 8, "小型股相對轉弱", f"IWM/SPY 20日={pct(s20)}，風險胃口下降")

    for t, s in [("00662", t00662), ("00670L", t00670)]:
        if len(s) >= 120:
            dist60 = s.iloc[-1] / ma(s, 60) - 1 if ma(s, 60) else np.nan
            rr = rsi(s)
            if not pd.isna(dist60) and dist60 > 0.12:
                score += add_signal(signals, 8, f"{t} 短線過熱", f"高於60日均線 {pct(dist60)}")
            if not pd.isna(rr) and rr > 76:
                score += add_signal(signals, 6, f"{t} RSI 過熱", f"RSI={num(rr)}")

    notes.append("本模組是市場廣度/貪婪 proxy，不等於官方 CNN Fear & Greed。好處是可自動化，壞處是少了情緒面雜訊。")
    return RadarResult("市場廣度 / 貪婪 proxy 雷達", min(score, max_score), max_score, signals, notes)



# ------------------------------------------------------------
# V13.2 私募信貸壓力雷達 / Private Credit Stress proxy
# ------------------------------------------------------------
def score_private_credit_stress(df: pd.DataFrame) -> RadarResult:
    """Private credit is opaque, so this is a proxy stress radar.

    It watches adjacent public markets: HY OAS, senior loan ETFs, BDC proxies,
    CLO ETF proxy, and private-credit related news keywords.
    """
    score = 0
    max_score = 100
    signals: List[str] = []
    notes: List[str] = []

    # 1) FRED HY OAS hard data. Unit: percentage points.
    hy_oas = fetch_fred_series("BAMLH0A0HYM2")
    if len(hy_oas) >= 20:
        latest = float(hy_oas.iloc[-1])
        chg20 = latest - float(hy_oas.iloc[-20])
        notes.append(f"HY OAS：{hy_oas.index[-1].date()} | {latest:.2f}% | 20筆變化 {chg20:.2f}pct")
        if latest > 8.0:
            score += add_signal(signals, 18, "高收益債利差危機區", f"HY OAS={latest:.2f}%")
        elif latest > 6.0:
            score += add_signal(signals, 12, "高收益債利差壓力明顯", f"HY OAS={latest:.2f}%")
        elif latest > 4.5:
            score += add_signal(signals, 7, "高收益債利差升溫", f"HY OAS={latest:.2f}%")
        if chg20 > 1.0:
            score += add_signal(signals, 10, "HY OAS 快速擴大", f"20筆 +{chg20:.2f}pct")
        elif chg20 > 0.5:
            score += add_signal(signals, 6, "HY OAS 溫和擴大", f"20筆 +{chg20:.2f}pct")
    else:
        notes.append("HY OAS（BAMLH0A0HYM2）資料不足；FRED/API 若暫時失敗則略過。")

    def _etf(name: str) -> pd.Series:
        try:
            return series(df, "Close", name)
        except Exception:
            return pd.Series(dtype=float)

    def _ma(s: pd.Series, n: int) -> float:
        try:
            return float(s.rolling(n).mean().iloc[-1]) if len(s) >= n else np.nan
        except Exception:
            return np.nan

    # 2) Senior loan ETFs: BKLN / SRLN
    for tk in ["SRLN"]:
        s = _etf(tk)
        if len(s) >= 60:
            r20 = ret(s, 20)
            ma60 = _ma(s, 60)
            notes.append(f"{tk} Senior Loan proxy：最新 {s.iloc[-1]:.2f} | 20日 {pct(r20)} | {'低於' if s.iloc[-1] < ma60 else '高於'}60日線")
            if s.iloc[-1] < ma60:
                score += add_signal(signals, 7, f"{tk} 跌破60日線", "槓桿貸款 / senior loan proxy 轉弱")
            if not pd.isna(r20) and r20 < -0.02:
                score += add_signal(signals, 7, f"{tk} 20日下跌", f"{pct(r20)}")
        elif len(s) > 0:
            notes.append(f"{tk} 資料不足，暫不計分；此為輔助資料，不影響 Data Health 主控。")

    # 3) CLO proxy: JBBB vs JAAA
    jaaa = _etf("JAAA")
    jbbb = _etf("JBBB")
    if len(jaaa) >= 40 and len(jbbb) >= 40:
        ratio = (jbbb / jaaa).dropna()
        if len(ratio) >= 20:
            r20 = ret(ratio, 20)
            notes.append(f"JBBB/JAAA CLO proxy：{ratio.iloc[-1]:.4f} | 20日 {pct(r20)}")
            if not pd.isna(r20) and r20 < -0.015:
                score += add_signal(signals, 8, "JBBB 弱於 JAAA", f"CLO BBB tranche proxy 20日 {pct(r20)}")
            if len(ratio) >= 60 and ratio.iloc[-1] < ratio.rolling(60).mean().iloc[-1]:
                score += add_signal(signals, 5, "JBBB/JAAA 低於60日線", "CLO 風險層相對轉弱")
    else:
        notes.append("JAAA/JBBB 資料不足，CLO proxy 暫不計分；此為私募信貸輔助資料，不影響 Data Health 主控。")

    # 4) BDC / listed private-credit proxy: BIZD and major BDC names
    bizd = _etf("BIZD")
    hyg = _etf("HYG")
    if len(bizd) >= 200:
        r20 = ret(bizd, 20)
        ma200 = _ma(bizd, 200)
        notes.append(f"BIZD BDC proxy：最新 {bizd.iloc[-1]:.2f} | 20日 {pct(r20)} | {'低於' if bizd.iloc[-1] < ma200 else '高於'}200日線")
        if bizd.iloc[-1] < ma200:
            score += add_signal(signals, 8, "BIZD 跌破200日線", "BDC / private-credit public proxy 轉弱")
        if not pd.isna(r20) and r20 < -0.08:
            score += add_signal(signals, 10, "BIZD 20日急跌", f"{pct(r20)}")
        if len(hyg) >= 20:
            rel = ret(bizd, 20) - ret(hyg, 20)
            if not pd.isna(rel) and rel < -0.04:
                score += add_signal(signals, 8, "BDC 明顯弱於 HYG", f"BIZD-HYG 20日相對 {pct(rel)}")
    elif len(bizd) > 0:
        notes.append("BIZD 資料不足，暫不計分；此為私募信貸輔助資料，不影響 Data Health 主控。")

    bdc_weak = 0
    bdc_total = 0
    for tk in ["FSK"]:
        s = _etf(tk)
        if len(s) >= 60:
            bdc_total += 1
            if s.iloc[-1] < _ma(s, 60):
                bdc_weak += 1
    if bdc_total:
        notes.append(f"主要 BDC 弱勢計數：{bdc_weak}/{bdc_total} 低於60日線")
        if bdc_total >= 3 and bdc_weak / bdc_total >= 0.6:
            score += add_signal(signals, 8, "BDC 集體轉弱", f"{bdc_weak}/{bdc_total} 低於60日線")

    # 5) News / topic stress keywords, using existing Google News helper if available.
    try:
        news_items = []
        if "fetch_google_news" in globals():
            for q in ["private credit redemption", "direct lending default", "BDC non-accrual", "private credit markdown", "leveraged loan distress"]:
                try:
                    news_items += fetch_google_news(q, limit=5)
                except TypeError:
                    news_items += fetch_google_news(q)
                except Exception:
                    continue
        text_blob = " ".join([str(x) for x in news_items])[:12000].lower()
        pc_keywords = ["private credit", "direct lending", "interval fund", "bcred", "cliffwater", "apollo", "blackstone", "leveraged loan", "clo", "bdc"]
        stress_keywords = ["default", "redemption", "gating", "non-accrual", "nonaccrual", "markdown", "liquidity", "distress", "loss"]
        pc_hits = sum(1 for kw in pc_keywords if kw in text_blob)
        stress_hits = sum(1 for kw in stress_keywords if kw in text_blob)
        if pc_hits or stress_hits:
            notes.append(f"私募信貸新聞關鍵字：pc_hits={pc_hits} / stress_hits={stress_hits}")
        if pc_hits >= 3 and stress_hits >= 2:
            score += add_signal(signals, 8, "私募信貸壓力新聞升溫", f"pc_hits={pc_hits}, stress_hits={stress_hits}")
        elif pc_hits >= 2 and stress_hits >= 1:
            score += add_signal(signals, 4, "私募信貸新聞觀察", f"pc_hits={pc_hits}, stress_hits={stress_hits}")
    except Exception:
        notes.append("私募信貸新聞 proxy 暫時無法讀取；不影響主要市場資料。")

    if not signals:
        signals.append("無明顯私募信貸壓力警訊")
    notes.append("本模組是 proxy：私募信貸不透明，因此以 HY OAS、Senior Loan、BDC、CLO proxy 與新聞壓力交叉觀察；單獨升溫不直接決定換倉。")
    return RadarResult("私募信貸壓力 proxy 雷達", min(score, max_score), max_score, signals, notes)

def score_taiwan_margin(df: pd.DataFrame) -> Tuple[RadarResult, pd.DataFrame]:
    signals, notes = [], []
    score, max_score = 0, 60

    margin = fetch_twse_margin_history()
    tpex_margin = fetch_tpex_margin_history()
    twii = series(df, "Close", "^TWII")

    if margin.empty or "margin_balance" not in margin.columns or margin["margin_balance"].dropna().shape[0] < 1:
        notes.append("台股融資資料抓取不足；本次不納入融資扣分。這一項才是真的資料源不足，不是市場沒警訊。")
        notes.append("V13 已支援 TWSE json / TWSE官方頁面 / Yahoo / WantGoo / 券商頁面備援，並會把抓到的資料累積到 tw_margin_history.csv。")
        if tpex_margin is not None and not tpex_margin.empty:
            tm = tpex_margin["margin_balance"].dropna()
            notes.append(f"櫃買融資輔助觀察：最新約 {tm.iloc[-1]:,.1f}（來源={str(tpex_margin.iloc[-1].get('source_group','TPEx'))}），不混入TWSE主數列。")
        return RadarResult("台股融資/槓桿雷達", score, max_score, signals, notes), margin

    if margin["margin_balance"].dropna().shape[0] < 5:
        m = margin["margin_balance"].dropna()
        latest = m.iloc[-1]
        src = ""
        if "source_group" in margin.columns and len(margin["source_group"].dropna()):
            src = str(margin["source_group"].dropna().iloc[-1])
        unit_hint = "億元" if ("third-party" in src or "official page latest" in src) else "交易單位/官方欄位"
        notes.append(f"台股融資餘額最新值：約 {latest:,.0f}（{unit_hint}，來源={src or 'TWSE'}）。")
        if "source" in margin.columns and len(margin["source"].dropna()):
            notes.append(f"資料來源備註：{str(margin['source'].dropna().iloc[-1])}")
        notes.append("目前本地累積資料少於5筆，趨勢不足，因此只顯示水位，不納入融資扣分；連續累積到5筆後會自動開始評分。")
        if tpex_margin is not None and not tpex_margin.empty:
            tm = tpex_margin["margin_balance"].dropna()
            if len(tm):
                notes.append(f"櫃買融資輔助觀察：最新約 {tm.iloc[-1]:,.1f}（來源={str(tpex_margin.iloc[-1].get('source_group','TPEx'))}），不混入TWSE主數列。")
        return RadarResult("台股融資/槓桿雷達", score, max_score, signals, notes), margin

    m = margin["margin_balance"].dropna()
    latest = m.iloc[-1]
    chg1 = latest / m.iloc[-2] - 1 if len(m) >= 2 and m.iloc[-2] else np.nan
    chg5 = latest / m.iloc[-5] - 1 if len(m) >= 5 and m.iloc[-5] else np.nan
    chg_all = latest / m.iloc[0] - 1 if len(m) >= 2 and m.iloc[0] else np.nan

    twii_r5 = ret(twii, 5) if len(twii) else np.nan
    twii_r20 = ret(twii, 20) if len(twii) else np.nan

    if not pd.isna(chg5) and chg5 > 0.03 and not pd.isna(twii_r5) and twii_r5 < 0:
        score += add_signal(signals, 14, "融資逆勢增加", f"融資5筆交易日={pct(chg5)}，但台股5日={pct(twii_r5)}")
    elif not pd.isna(chg5) and chg5 > 0.05:
        score += add_signal(signals, 9, "融資升溫", f"融資5筆交易日={pct(chg5)}")

    if not pd.isna(chg_all) and chg_all > 0.08:
        score += add_signal(signals, 8, "融資快速堆高", f"近抓取區間融資變化={pct(chg_all)}")

    if not pd.isna(chg5) and chg5 < -0.04 and not pd.isna(twii_r5) and twii_r5 < -0.03:
        score += add_signal(signals, 12, "融資退潮", f"融資5筆交易日={pct(chg5)}，台股5日={pct(twii_r5)}，可能出現去槓桿")

    if not pd.isna(chg1) and chg1 > 0.015:
        score += add_signal(signals, 4, "融資單日升溫", f"最新一筆變化={pct(chg1)}")

    src = ""
    if "source_group" in margin.columns and len(margin["source_group"].dropna()):
        src = str(margin["source_group"].dropna().iloc[-1])
    unit_hint = "億元" if "third-party" in src else "交易單位/官方欄位"
    notes.append(f"台股融資餘額最新值：約 {latest:,.0f}（{unit_hint}，來源={src or 'TWSE'}）；1筆變化 {pct(chg1)}；5筆變化 {pct(chg5)}")
    if "source" in margin.columns and len(margin["source"].dropna()):
        notes.append(f"資料來源備註：{str(margin['source'].dropna().iloc[-1])}")
    if not pd.isna(twii_r20):
        notes.append(f"加權指數20日變化：{pct(twii_r20)}")

    # TPEx auxiliary: separate OTC leverage observation. Do not merge into TWSE main series.
    try:
        if tpex_margin is not None and not tpex_margin.empty and "margin_balance" in tpex_margin.columns:
            tm = tpex_margin["margin_balance"].dropna()
            if len(tm) >= 1:
                latest_tpex = tm.iloc[-1]
                tchg1 = latest_tpex / tm.iloc[-2] - 1 if len(tm) >= 2 and tm.iloc[-2] else np.nan
                tchg5 = latest_tpex / tm.iloc[-5] - 1 if len(tm) >= 5 and tm.iloc[-5] else np.nan
                tsrc = str(tpex_margin.iloc[-1].get("source_group", "TPEx auxiliary"))
                unit_hint = "億元" if "auxiliary" in tsrc else "官方欄位"
                notes.append(f"櫃買融資輔助觀察：最新約 {latest_tpex:,.1f}（{unit_hint}，來源={tsrc}）；1筆 {pct(tchg1)}；5筆 {pct(tchg5)}。不混入TWSE主數列。")
                if not pd.isna(tchg5) and tchg5 > 0.05:
                    score += add_signal(signals, 4, "櫃買融資升溫", f"TPEx 5筆變化={pct(tchg5)}，輔助觀察")
                elif not pd.isna(tchg5) and tchg5 < -0.05:
                    score += add_signal(signals, 4, "櫃買融資退潮", f"TPEx 5筆變化={pct(tchg5)}，輔助觀察")
    except Exception:
        notes.append("櫃買融資輔助資料暫時無法解析；不影響TWSE主數列。")

    return RadarResult("台股融資/槓桿雷達", min(score, max_score), max_score, signals, notes), margin


# ------------------------------------------------------------
# V11 總控：警報分數、戰鬥模式、R模式、轉換條件
# ------------------------------------------------------------
def score_level(total_risk: float) -> Tuple[str, str]:
    if total_risk <= 30:
        return "🟢 綠色穩定", "正常"
    if total_risk <= 55:
        return "🟡 黃色過熱觀察", "過熱觀察"
    if total_risk <= 70:
        return "🟠 橘色危機升溫/防守避震", "危機升溫"
    if total_risk <= 85:
        return "🔴 紅色確認危機/高風險防守", "確認危機"
    return "🟣 紫色恐慌/流動性危機", "恐慌 / 流動性危機"


def decide_battle_mode_v11(total_risk: float, macro: RadarResult, credit: RadarResult, market: RadarResult, margin: RadarResult, breadth: RadarResult, df: pd.DataFrame) -> Tuple[str, str, List[str], List[str], bool]:
    reasons: List[str] = []
    next_watch: List[str] = []

    # V11-Core R7.2 戰鬥模式邏輯：
    # 452：平常作戰 / 中性偏進攻底盤。
    # 514：危機升溫 / 防守避震 / 降槓桿。
    # 433：R 模式確認 / 防守反擊。
    #
    # R7.2 核心修正：
    # - 平常保留 R7 的快速回攻能力。
    # - 只有信用危機型才啟動「防亂跳 gate」。
    # - 目的：2020 不龜太久；2008 / 2000 長熊不亂反攻。
    if total_risk <= 30:
        mode = "452"
        stance = "正常作戰：採 452 作為平常進攻底盤，維持趨勢曝險，但不因市場安靜就追高。"
    elif total_risk <= 55:
        mode = "452"
        stance = "過熱觀察：仍維持 452，但停止追高；市場可以續漲，但風險報酬開始變差。"
    elif total_risk <= 70:
        mode = "514"
        stance = "危機升溫 / 假訊號緩衝：切回 514 降槓桿與提高債券避震，先活著，不急著反攻。"
    elif total_risk <= 85:
        mode = "514"
        stance = "確認危機 / 高風險防守：維持 514 防守，避免在波動最大時被迫賣出。"
    else:
        mode = "514"
        stance = "恐慌/流動性危機：維持 514 保命，等待 R 模式或明確反攻條件，不要 all in 當英雄。"

    # 強制切換條件：分數未到，但信用、宏觀、動能同步惡化時，不等總分慢慢加上來。
    if credit.risk_pct >= 65 and macro.risk_pct >= 55 and total_risk < 71:
        mode = "514"
        stance = "信用與宏觀同步惡化，提前切到 514 防守。這不是單一雜訊，而是風險開始擴散。"
        reasons.append("信用利差 proxy 與宏觀壓力同步惡化，提前進入 514 防守避震。")
    elif credit.risk_pct >= 55 and market.risk_pct >= 55 and total_risk < 56:
        mode = "514"
        stance = "信用與股市同步轉弱，提前切到 514 觀察。這可能是假訊號，但不能完全不理。"
        reasons.append("信用與股市動能同時轉弱，先進入 514 降槓桿緩衝模式。")

    # 固定輸出原因
    if macro.risk_pct >= 45:
        reasons.append(f"宏觀壓力偏高：{macro.name} {macro.score}/{macro.max_score}。")
    if credit.risk_pct >= 45:
        reasons.append(f"信用利差 proxy 轉弱：{credit.name} {credit.score}/{credit.max_score}。")
    if market.risk_pct >= 45:
        reasons.append(f"股市動能轉弱：{market.name} {market.score}/{market.max_score}。")
    if margin.risk_pct >= 40:
        reasons.append(f"台股融資/槓桿升溫或退潮：{margin.name} {margin.score}/{margin.max_score}。")
    if breadth.risk_pct >= 45:
        reasons.append(f"市場廣度或貪婪 proxy 出現警訊：{breadth.name} {breadth.score}/{breadth.max_score}。")
    if not reasons:
        reasons.append("核心壓力尚未擴散，維持目前模式即可。")

    # R7.2 R 模式：衛星反攻 / 確認反攻。
    vix = series(df, "Close", "^VIX")
    qqq = series(df, "Close", "QQQ")
    soxx = series(df, "Close", "SOXX")
    hyg = series(df, "Close", "HYG")
    lqd = series(df, "Close", "LQD")
    t00662 = series(df, "Close", "00662.TW")
    tnx = series(df, "Close", "^TNX") / 10.0

    rebound = False
    rebound_hits = []

    # 信用危機型 gate：只在 HYG/LQD 出現深度回撤時啟動。
    # backtest R7.2 用 252 日高點回撤與 90 日內 -18% 當 proxy。
    credit_crisis_regime = False
    credit_gate_safe = True
    if len(hyg) >= 252 and len(lqd) >= 252:
        credit_ratio = (hyg / lqd).dropna()
        if len(credit_ratio) >= 252:
            credit_high_252 = credit_ratio.rolling(252, min_periods=60).max()
            credit_dd_252 = credit_ratio / credit_high_252 - 1.0
            recent_deep_credit_dd = credit_dd_252.tail(90).min()
            credit_crisis_regime = bool(pd.notna(recent_deep_credit_dd) and recent_deep_credit_dd <= -0.18)
            credit_gate_safe = (not credit_crisis_regime) or (total_risk < 75 and credit.risk_pct < 60)

    if len(vix) >= 10 and vix.iloc[-5:].max() > 35 and vix.iloc[-1] < vix.iloc[-5:].max() * 0.90:
        rebound_hits.append("VIX 高檔開始回落")
    if len(qqq) >= 60 and qqq.iloc[-1] > ma(qqq, 20):
        rebound_hits.append("Nasdaq / QQQ 站回短期均線")
    if len(soxx) >= 60 and soxx.iloc[-1] > ma(soxx, 20):
        rebound_hits.append("SOXX 站回短期均線")
    if len(hyg) >= 60 and len(lqd) >= 60:
        ratio = (hyg / lqd).dropna()
        if len(ratio) >= 20 and ret(ratio, 5) > 0:
            rebound_hits.append("HYG/LQD 不再下跌")
    if len(tnx) >= 10 and diff(tnx, 5) < 0.05:
        rebound_hits.append("美債殖利率停止急升")
    if len(t00662) >= 120 and t00662.iloc[-1] > ma(t00662, 60):
        rebound_hits.append("00662 接近或站回中期均線")

    if credit_crisis_regime:
        reasons.append("R7.2 信用危機 gate：HYG/LQD 曾出現深度回撤，解除防守需更嚴格。")

    if mode == "514" and len(rebound_hits) >= 4:
        if credit_gate_safe:
            rebound = True
            mode = "433"
            stance = "R模式 / 確認反攻：恐慌後的反攻條件初步成立，切到 433；仍應分批，不要一次 all in。"
            reasons.append("R7.2 R模式條件成立：" + "、".join(rebound_hits[:4]) + "，由 514 轉入 433 防守反擊。")
        else:
            reasons.append("R7.2：反彈訊號已出現，但信用危機 gate 尚未解除，暫維持 514，避免熊市反彈假訊號。")

    # 下一步觀察條件
    if mode == "452":
        next_watch.append("若總分升破 56，或 VIX > 25 且 HYG/LQD 跌破60日，452 → 514。")
        next_watch.append("若 VIX < 20 且信用穩定，但 00670L/QQQ 過熱，維持 452，但停止追高。")
    elif mode == "514":
        next_watch.append("若 VIX > 35 後開始回落、Nasdaq/SOXX 止跌、HYG/LQD 不再下跌，可由 514 → 433 啟動 R模式。")
        next_watch.append("R7.2：若屬信用危機型，需等總分 < 75 且信用分數降溫，才解除防亂跳 gate。")
        next_watch.append("若總分降回 55 以下且信用穩定，但尚未出現反攻訊號，514 → 452。")
    else:  # 433
        next_watch.append("若反攻失敗、信用利差再轉弱或 VIX 重新急升，433 → 514。")
        next_watch.append("若趨勢回穩且市場不再恐慌，433 可逐步回到 452 平常作戰。")

    return mode, stance, reasons, next_watch, rebound



def latest_valid_date_for_ticker(df: pd.DataFrame, ticker: str) -> Optional[pd.Timestamp]:
    """Return latest valid price date for ticker in yfinance multi-index close data."""
    try:
        s = series(df, "Close", ticker)
        if s is None or len(s.dropna()) == 0:
            return None
        return pd.Timestamp(s.dropna().index[-1])
    except Exception:
        return None


def tw_trading_day_age(date: Optional[pd.Timestamp], ref: Optional[pd.Timestamp] = None) -> Optional[int]:
    """Business-day based age for data-health gating.

    This avoids false stale warnings over weekends and most non-trading gaps.
    It does not know every US/TW holiday, but it is much better than raw calendar days.
    """
    if date is None or pd.isna(date):
        return None
    if ref is None:
        ref = TODAY

    d = pd.Timestamp(date)
    r = pd.Timestamp(ref)

    if d.tzinfo is not None:
        d = d.tz_convert(None)
    if r.tzinfo is not None:
        r = r.tz_convert(None)

    d0 = d.normalize().date()
    r0 = r.normalize().date()
    if r0 <= d0:
        return 0
    try:
        return int(np.busday_count(d0, r0))
    except Exception:
        return max(0, int((pd.Timestamp(r0) - pd.Timestamp(d0)).days))


def latest_margin_date(margin_df: pd.DataFrame) -> Optional[pd.Timestamp]:
    try:
        if margin_df is None or margin_df.empty or "date" not in margin_df.columns:
            return None
        dates = pd.to_datetime(margin_df["date"].astype(str), format="%Y%m%d", errors="coerce").dropna()
        if len(dates) == 0:
            return None
        return pd.Timestamp(dates.max())
    except Exception:
        return None


def data_health_check(df: pd.DataFrame, margin_df: pd.DataFrame) -> Tuple[bool, List[str], List[str], Dict[str, object]]:
    """Check whether core data is fresh enough to allow OS mode switches.

    Returns:
    - healthy_to_switch: bool
    - health_lines: concise Telegram lines
    - warnings: warning lines
    - status: machine-readable dict
    """
    warnings: List[str] = []
    health_lines: List[str] = []
    status: Dict[str, object] = {"core": {}, "secondary": {}, "margin": {}}

    core_bad = []
    secondary_bad = []

    for t in DATA_HEALTH_CORE_TICKERS:
        dt = latest_valid_date_for_ticker(df, t)
        age = tw_trading_day_age(dt)
        ok = (age is not None) and (age <= DATA_HEALTH_MAX_CORE_STALE_DAYS)
        status["core"][t] = {"latest_date": dt.strftime("%Y-%m-%d") if dt is not None else "N/A", "age_days": age, "ok": ok}
        if not ok:
            core_bad.append(t)

    for t in DATA_HEALTH_SECONDARY_TICKERS:
        dt = latest_valid_date_for_ticker(df, t)
        age = tw_trading_day_age(dt)
        ok = (age is not None) and (age <= DATA_HEALTH_MAX_CORE_STALE_DAYS + 1)
        status["secondary"][t] = {"latest_date": dt.strftime("%Y-%m-%d") if dt is not None else "N/A", "age_days": age, "ok": ok}
        if not ok:
            secondary_bad.append(t)

    mdt = latest_margin_date(margin_df)
    mage = tw_trading_day_age(mdt)
    margin_ok = (mage is not None) and (mage <= DATA_HEALTH_MAX_MARGIN_STALE_DAYS)
    status["margin"] = {"latest_date": mdt.strftime("%Y-%m-%d") if mdt is not None else "N/A", "age_days": mage, "ok": margin_ok}

    # Summary lines: keep it short, but informative.
    core_summary = []
    for t in DATA_HEALTH_CORE_TICKERS:
        item = status["core"][t]
        core_summary.append(f"{t}:{item['latest_date']}")
    health_lines.append("核心資料日期：" + " / ".join(core_summary[:4]))
    health_lines.append("核心資料日期：" + " / ".join(core_summary[4:]))
    health_lines.append(f"台股融資資料日期：{status['margin']['latest_date']}（延遲 {status['margin']['age_days']} 天）")

    if core_bad:
        warnings.append("核心資料延遲或缺漏：" + "、".join(core_bad))
    if secondary_bad:
        warnings.append("輔助資料延遲或缺漏：" + "、".join(secondary_bad[:8]))
    if not margin_ok:
        warnings.append("台股融資資料延遲或缺漏；本次融資趨勢僅供參考。")

    healthy_to_switch = len(core_bad) == 0

    if healthy_to_switch:
        health_lines.insert(0, "資料健康檢查：✅ 核心資料同步正常，允許模式切換。")
    else:
        health_lines.insert(0, "資料健康檢查：⚠️ 核心資料不同步，本次禁止模式切換，只維持原模式。")

    return healthy_to_switch, health_lines, warnings, status


def load_os31_state(path: str = OS31_STATE_FILE) -> Dict[str, object]:
    """Load OS 3.1.1 state. GitHub Actions persists this through a committed json file."""
    default = {
        "current_mode": "452",
        "crisis_memory": False,
        "hold_433_weeks": 0,
        "last_raw_signal": "452",
        "last_final_mode": "452",
        "last_update": "",
    }
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        default.update(data)
        if default.get("current_mode") not in ["452", "514", "433"]:
            default["current_mode"] = "452"
        default["crisis_memory"] = bool(default.get("crisis_memory", False))
        default["hold_433_weeks"] = int(default.get("hold_433_weeks", 0) or 0)
        return default
    except Exception:
        return default


def save_os31_state(state: Dict[str, object], path: str = OS31_STATE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def apply_os311_state_machine(raw_mode: str, raw_stance: str, state: Dict[str, object], data_health_ok: bool = True, data_health_warnings: Optional[List[str]] = None) -> Tuple[str, str, List[str], Dict[str, object]]:
    """
    OS 3.1.1 operation state layer:
    - R7.2 raw signal judges market.
    - OS 3.1.1 decides executable final mode.
    - 433 requires crisis_memory=True.
    - 433 has minimum 8-week hold unless raw signal returns to 514.
    """
    current_mode = str(state.get("current_mode", "452"))
    crisis_memory = bool(state.get("crisis_memory", False))
    hold_433_weeks = int(state.get("hold_433_weeks", 0) or 0)

    final_mode = current_mode
    os_reasons: List[str] = []

    if not data_health_ok:
        # Data gate: never switch modes on stale / missing core data.
        final_mode = current_mode
        os_reasons.append("OS 3.1.1 Data Health Gate：核心資料不同步，本次不執行模式切換，維持原模式。")
        for w in (data_health_warnings or []):
            os_reasons.append("資料警告：" + w)
        new_state = {
            "current_mode": final_mode,
            "crisis_memory": crisis_memory,
            "hold_433_weeks": hold_433_weeks,
            "last_raw_signal": raw_mode,
            "last_final_mode": final_mode,
            "last_update": TODAY.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
        final_stance = f"資料健康檢查未通過，OS 3.1.1 最終操作模式維持 {final_mode}；R7.2 原始訊號為 {raw_mode}。"
        return final_mode, final_stance, os_reasons, new_state

    if raw_mode == "514":
        final_mode = "514"
        crisis_memory = True
        hold_433_weeks = 0
        os_reasons.append("OS 3.1.1：原始訊號=514，進入危機防守，crisis_memory=True。")

    elif current_mode == "433" and raw_mode != "514" and hold_433_weeks < OS31_MIN_433_HOLD_WEEKS:
        final_mode = "433"
        hold_433_weeks += 1
        os_reasons.append(
            f"OS 3.1.1：目前已在433，且尚未滿最短持有 {OS31_MIN_433_HOLD_WEEKS} 週，維持433（第 {hold_433_weeks}/{OS31_MIN_433_HOLD_WEEKS} 週）。"
        )

    elif raw_mode == "433":
        if crisis_memory:
            final_mode = "433"
            hold_433_weeks = 1 if current_mode != "433" else max(hold_433_weeks + 1, 1)
            os_reasons.append(
                f"OS 3.1.1：原始訊號=433，且 crisis_memory=True，允許危機後反攻；433持有週數={hold_433_weeks}。"
            )
        else:
            final_mode = "452"
            hold_433_weeks = 0
            os_reasons.append("OS 3.1.1：原始訊號=433，但 crisis_memory=False，尚未經歷514，不啟動433，維持452。")

    else:  # raw_mode == 452
        if current_mode == "433" and hold_433_weeks < OS31_MIN_433_HOLD_WEEKS:
            final_mode = "433"
            hold_433_weeks += 1
            os_reasons.append(
                f"OS 3.1.1：原始訊號=452，但433尚未滿最短持有 {OS31_MIN_433_HOLD_WEEKS} 週，維持433（第 {hold_433_weeks}/{OS31_MIN_433_HOLD_WEEKS} 週）。"
            )
        else:
            final_mode = "452"
            crisis_memory = False
            hold_433_weeks = 0
            os_reasons.append("OS 3.1.1：原始訊號=452，回到平常作戰，crisis_memory=False。")

    new_state = {
        "current_mode": final_mode,
        "crisis_memory": crisis_memory,
        "hold_433_weeks": hold_433_weeks,
        "last_raw_signal": raw_mode,
        "last_final_mode": final_mode,
        "last_update": TODAY.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }

    if final_mode == raw_mode:
        final_stance = raw_stance
    else:
        final_stance = f"OS 3.1.1 最終操作模式為 {final_mode}；R7.2 原始訊號為 {raw_mode}。"

    return final_mode, final_stance, os_reasons, new_state


def format_mode(mode_name: str) -> str:
    w = normalize_mode(BATTLE_MODES[mode_name])
    parts = []
    for t, weight in w.items():
        label = TICKERS.get(t, t)
        parts.append(f"{label} {weight * 100:.1f}%")
    return " / ".join(parts)


def daily_snapshot(df: pd.DataFrame, margin_df: pd.DataFrame) -> List[str]:
    lines = []

    def add_ret_line(label: str, ticker: str, as_yield: bool = False) -> None:
        s = series(df, "Close", ticker)
        if len(s) < 2:
            lines.append(f"- {label}：N/A")
            return
        if as_yield:
            sy = s / 10.0
            lines.append(f"- {label}：{num(sy.iloc[-2])}% → {num(sy.iloc[-1])}%（{bp(diff(sy, 1))}）")
        else:
            lines.append(f"- {label}：{num(s.iloc[-2])} → {num(s.iloc[-1])}（{pct(ret(s, 1))}）")

    add_ret_line("VIX", "^VIX")
    # HYG/LQD
    hyg, lqd = series(df, "Close", "HYG"), series(df, "Close", "LQD")
    if len(hyg) >= 2 and len(lqd) >= 2:
        ratio = (hyg / lqd).dropna()
        lines.append(f"- HYG/LQD：{num(ratio.iloc[-2],4)} → {num(ratio.iloc[-1],4)}（{pct(ret(ratio, 1))}）")
    else:
        lines.append("- HYG/LQD：N/A")
    add_ret_line("日圓 USDJPY", "JPY=X")
    add_ret_line("美債10Y", "^TNX", as_yield=True)
    add_ret_line("00662", "00662.TW")
    add_ret_line("00670L", "00670L.TW")
    add_ret_line("00865B", "00865B.TW")

    if not margin_df.empty and "margin_balance" in margin_df.columns and margin_df["margin_balance"].dropna().shape[0] >= 2:
        m = margin_df["margin_balance"].dropna()
        lines.append(f"- 台股融資：{m.iloc[-2]:,.0f} → {m.iloc[-1]:,.0f}（{pct(m.iloc[-1] / m.iloc[-2] - 1)}）")
    else:
        lines.append("- 台股融資：N/A")
    return lines



# ------------------------------------------------------------
# V13.5 歷史紀錄 / 趨勢追蹤
# ------------------------------------------------------------
RADAR_SCORE_HISTORY_FILE = "storage/radar_score_history.csv"
MODULE_SCORE_HISTORY_FILE = "storage/module_score_history.csv"
MARKET_BACKFILL_90D_FILE = "storage/market_backfill_90d.csv"
TREND_SUMMARY_FILE = "storage/radar_trend_summary.json"


def _safe_latest(s: pd.Series) -> float:
    try:
        s = s.dropna()
        if len(s) == 0:
            return np.nan
        return float(s.iloc[-1])
    except Exception:
        return np.nan


def write_market_backfill_90d(df: pd.DataFrame) -> None:
    """Backfill what can be backfilled from yfinance: prices/ratios for recent ~90 trading days.

    News modules cannot be truly backfilled, so they only accumulate from live runs.
    """
    try:
        os.makedirs("storage", exist_ok=True)
        tickers = ["^VIX", "QQQ", "SMH", "HYG", "LQD", "JNK", "TLT", "^TNX", "JPY=X",
                   "00662.TW", "00670L.TW", "00865B.TW", "^TWII"]
        rows = []
        # Use QQQ dates as base if available.
        base = series(df, "Close", "QQQ")
        if len(base) == 0:
            return
        dates = list(base.dropna().index)[-90:]
        for dt in dates:
            row = {"date": pd.Timestamp(dt).strftime("%Y-%m-%d")}
            for tk in tickers:
                s = series(df, "Close", tk)
                try:
                    row[tk] = float(s.loc[:dt].dropna().iloc[-1])
                except Exception:
                    row[tk] = np.nan
            try:
                row["HYG_LQD"] = row["HYG"] / row["LQD"] if row.get("HYG") and row.get("LQD") else np.nan
            except Exception:
                row["HYG_LQD"] = np.nan
            try:
                row["JNK_LQD"] = row["JNK"] / row["LQD"] if row.get("JNK") and row.get("LQD") else np.nan
            except Exception:
                row["JNK_LQD"] = np.nan
            rows.append(row)
        if rows:
            pd.DataFrame(rows).to_csv(MARKET_BACKFILL_90D_FILE, index=False)
    except Exception as e:
        print("write_market_backfill_90d failed:", e)


def append_radar_history(total_risk: float,
                         mode: str,
                         raw_mode: str,
                         data_health_ok: bool,
                         results: List[RadarResult],
                         df: pd.DataFrame,
                         margin_df: pd.DataFrame) -> List[str]:
    """Append current run score/module history and return trend summary lines."""
    import json

    os.makedirs("storage", exist_ok=True)
    now_iso = TODAY.isoformat(timespec="seconds")
    row = {
        "time_taipei": now_iso,
        "date": TODAY.strftime("%Y-%m-%d"),
        "total_risk": round(float(total_risk), 4),
        "mode": mode,
        "raw_mode": raw_mode,
        "data_health_ok": bool(data_health_ok),
        "vix": _safe_latest(series(df, "Close", "^VIX")),
        "qqq": _safe_latest(series(df, "Close", "QQQ")),
        "hyg_lqd": np.nan,
        "usdjpy": _safe_latest(series(df, "Close", "JPY=X")),
        "twii": _safe_latest(series(df, "Close", "^TWII")),
        "tw_margin": np.nan,
    }
    try:
        row["hyg_lqd"] = _safe_latest(series(df, "Close", "HYG")) / _safe_latest(series(df, "Close", "LQD"))
    except Exception:
        pass
    try:
        if margin_df is not None and not margin_df.empty and "margin_balance" in margin_df.columns:
            row["tw_margin"] = float(pd.to_numeric(margin_df["margin_balance"], errors="coerce").dropna().iloc[-1])
    except Exception:
        pass

    for res in results:
        key = str(res.name).replace(" ", "_").replace("/", "_").replace("｜", "_")
        row[f"{key}_score"] = res.score
        row[f"{key}_risk_pct"] = round(float(res.risk_pct), 4)

    try:
        old = pd.read_csv(RADAR_SCORE_HISTORY_FILE) if os.path.exists(RADAR_SCORE_HISTORY_FILE) else pd.DataFrame()
        hist = pd.concat([old, pd.DataFrame([row])], ignore_index=True)
        # keep last 400 runs, avoid exact duplicate timestamp
        hist = hist.drop_duplicates(subset=["time_taipei"], keep="last").tail(400)
        hist.to_csv(RADAR_SCORE_HISTORY_FILE, index=False)
    except Exception as e:
        print("append radar history failed:", e)
        hist = pd.DataFrame([row])

    # separate compact module history
    try:
        mod_rows = []
        for res in results:
            mod_rows.append({
                "time_taipei": now_iso,
                "date": TODAY.strftime("%Y-%m-%d"),
                "module": res.name,
                "score": res.score,
                "max_score": res.max_score,
                "risk_pct": round(float(res.risk_pct), 4),
                "signals": " | ".join(res.signals[:5]) if res.signals else "",
            })
        oldm = pd.read_csv(MODULE_SCORE_HISTORY_FILE) if os.path.exists(MODULE_SCORE_HISTORY_FILE) else pd.DataFrame()
        mhist = pd.concat([oldm, pd.DataFrame(mod_rows)], ignore_index=True).tail(3000)
        mhist.to_csv(MODULE_SCORE_HISTORY_FILE, index=False)
    except Exception as e:
        print("append module history failed:", e)

    trend_lines = build_radar_trend_summary(hist)
    try:
        with open(TREND_SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump({"time_taipei": now_iso, "trend_lines": trend_lines}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return trend_lines


def _trend_word(delta: float) -> str:
    if pd.isna(delta):
        return "資料不足"
    if delta >= 10:
        return "明顯升溫"
    if delta >= 3:
        return "緩慢升溫"
    if delta <= -10:
        return "明顯降溫"
    if delta <= -3:
        return "緩慢降溫"
    return "大致持平"


def build_radar_trend_summary(hist: pd.DataFrame) -> List[str]:
    try:
        if hist is None or hist.empty:
            return ["歷史紀錄剛開始建立，尚無趨勢。"]
        hist = hist.copy()
        hist["total_risk"] = pd.to_numeric(hist.get("total_risk"), errors="coerce")
        n = len(hist)
        if n < 2:
            return ["歷史紀錄第 1 筆：先建立基準；新聞類雷達會從現在開始累積。"]

        latest = float(hist["total_risk"].iloc[-1])
        prev = float(hist["total_risk"].iloc[-2])
        first = float(hist["total_risk"].iloc[0])
        delta_1 = latest - prev
        delta_all = latest - first
        window = min(20, n)
        start20 = float(hist["total_risk"].iloc[-window])
        delta20 = latest - start20

        lines = [
            f"已累積 {n} 筆雷達紀錄；總分 {first:.1f} → {latest:.1f}（累積 {delta_all:+.1f}）。",
            f"近 {window} 筆總分 {start20:.1f} → {latest:.1f}：{_trend_word(delta20)}（{delta20:+.1f}）。",
            f"最近一筆變化：{prev:.1f} → {latest:.1f}（{delta_1:+.1f}）。",
        ]

        # highlight module trends from risk_pct columns
        risk_cols = [c for c in hist.columns if c.endswith("_risk_pct")]
        module_moves = []
        for c in risk_cols:
            try:
                s = pd.to_numeric(hist[c], errors="coerce").dropna()
                if len(s) >= 2:
                    w = min(20, len(s))
                    d = float(s.iloc[-1] - s.iloc[-w])
                    if abs(d) >= 5:
                        name = c.replace("_risk_pct", "").replace("_", " ")
                        module_moves.append((abs(d), name, d, float(s.iloc[-1])))
            except Exception:
                continue
        module_moves.sort(reverse=True)
        if module_moves:
            for _, name, d, val in module_moves[:3]:
                lines.append(f"{name}：近 {window} 筆 {d:+.1f}pct，目前 {val:.1f}%。")
        else:
            lines.append("各模組目前沒有明顯連續升溫；若新聞很熱但硬資料冷，先列為觀察，不直接換檔。")

        lines.append("可回補的市場價格已寫入 market_backfill_90d.csv；新聞/題材類無法可靠回補，將從現在開始慢慢累積。")
        return lines
    except Exception as e:
        return [f"趨勢摘要暫時無法產生：{type(e).__name__}"]

def format_result(results: List[RadarResult], total_risk: float, mode: str, stance: str, reasons: List[str], next_watch: List[str], rebound: bool, df: pd.DataFrame, margin_df: pd.DataFrame, raw_mode: str = '', os_state: Optional[Dict[str, object]] = None, data_health_lines: Optional[List[str]] = None, data_health_warnings: Optional[List[str]] = None, trend_lines: Optional[List[str]] = None) -> str:
    now = TODAY.strftime("%Y-%m-%d %H:%M")
    level, level_name = score_level(total_risk)
    lines: List[str] = []

    lines.append("🌐 全球市場雷達 v13.5｜總控 + 產業輪動 + Reddit 題材整合版")
    lines.append(f"時間：{now}（台北）")
    lines.append("")
    lines.append(f"市場風險總分：{total_risk:.1f}/100")
    lines.append(f"等級：{level}")
    lines.append("")
    lines.append("📌 加權總分摘要")
    lines.append("總分權重：宏觀20% / 信用20% / 動能20% / Fed10% / 私募信貸10% / 融資10% / 廣度10%")
    lines.append("提醒：亞洲槓桿壓力 beta 只提醒，不納入總分。")
    if trend_lines:
        lines.append("")
        lines.append("📈 風險趨勢追蹤")
        for x in trend_lines[:7]:
            lines.append(f"- {x}")
    if raw_mode:
        lines.append(f"V13.5 原始訊號：{raw_mode}")
    lines.append(f"OS 3.1.1 最終操作模式：{mode}")
    lines.append(f"配置比例：{format_mode(mode)}")
    if os_state is not None:
        lines.append(f"crisis_memory：{os_state.get('crisis_memory')}")
        lines.append(f"433持有週數：{os_state.get('hold_433_weeks', 0)}/{OS31_MIN_433_HOLD_WEEKS}")
    if rebound:
        lines.append("R模式：✅ 衛星反攻條件初步成立，可分 3～5 批買回公司/衛星倉，不要一次 all in。")
    else:
        lines.append("R模式：尚未啟動。")

    if data_health_lines:
        lines.append("")
        lines.append("📡 資料同步狀態")
        for x in data_health_lines:
            lines.append(f"- {x}")
        if data_health_warnings:
            for x in data_health_warnings[:4]:
                lines.append(f"- ⚠️ {x}")

    lines.append(f"操作摘要：{stance}")
    lines.append("")

    lines.append("📊 昨天 vs 今天")
    lines.extend(daily_snapshot(df, margin_df))
    lines.append("")

    lines.append("🧠 為什麼是這個戰鬥模式")
    for r in reasons[:8]:
        lines.append(f"- {r}")
    lines.append("結論：" + stance)
    lines.append("")

    lines.append("🔁 下一步觀察 / 改變模式條件")
    for w in next_watch:
        lines.append(f"- {w}")
    lines.append("")

    lines.append("📌 關鍵快照")
    snapshot_keys = ["^VIX", "^MOVE", "^TNX", "JPY=X", "GLD", "TLT", "HYG", "LQD", "QQQ", "SMH", "RSP", "IWM", "00662.TW", "00670L.TW", "00865B.TW", "^TWII"]
    for t in snapshot_keys:
        s = series(df, "Close", t)
        if len(s):
            val = s.iloc[-1]
            if t in ["^TNX", "^TYX"]:
                lines.append(f"- {TICKERS.get(t, t)}：{num(val / 10)}%｜20日 {bp(diff(s/10.0, 20))}")
            else:
                lines.append(f"- {TICKERS.get(t, t)}：{num(val)}｜20日 {pct(ret(s, 20))}")
    lines.append("")

    for res in results:
        lines.append(f"【{res.name}】{res.score}/{res.max_score}（{res.risk_pct:.1f}%）")
        if res.signals:
            for sig in res.signals[:10]:
                lines.append(f"- {sig}")
            if len(res.signals) > 10:
                lines.append(f"- ……另有 {len(res.signals) - 10} 項訊號")
        else:
            lines.append("- 無明顯警訊")
        if res.notes:
            for note in res.notes[:4]:
                lines.append(f"  備註：{note}")
        lines.append("")

    lines.append("🧭 分數與戰鬥模式")
    lines.append("- 0～30：正常偏多 → 452")
    lines.append("- 31～55：過熱觀察 → 452，但停止追高")
    lines.append("- 56～70：危機升溫 / 假訊號緩衝 → 514")
    lines.append("- 71～85：確認危機 / 高風險防守 → 514")
    lines.append("- 86～100：恐慌/流動性危機 → 514，等待 R模式")
    lines.append("")
    lines.append("🛰 R模式規則")
    lines.append("- 452 是平常作戰 / 中性偏進攻底盤，不是防守檔。")
    lines.append("- 514 是危機升溫時的防守避震 / 降槓桿模式。")
    lines.append("- 433 是 R模式 / 危機後確認反攻；OS 3.1.1 規定沒有 crisis_memory 不啟動 433。")
    lines.append("- 主要觸發：VIX > 35 後回落、Nasdaq/SOXX 止跌、HYG/LQD 不再下跌、美債殖利率停止急升、00662 接近長期均線；433 最短持有 8 週，除非重新切 514。")
    lines.append("")
    lines.append("提醒：你的 V13.5 是飛機儀表板，不是自動駕駛。它能告訴你高度、風速、燃料、引擎溫度；最後拉桿的人還是你。")
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Print only.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    text = message
    chunks = []
    while len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        if cut <= 0:
            cut = 3900
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    chunks.append(text)

    for chunk in chunks:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text)
        time.sleep(0.5)



# ------------------------------------------------------------
# V12：產業輪動 / 題材 / Reddit + News 模組
# ------------------------------------------------------------
THEME_BASKETS = {
    "AI伺服器": ["2382.TW", "3231.TW", "2356.TW"],
    "電源與重電": ["2308.TW", "1503.TW", "1513.TW", "1519.TW"],
    "環保回收": ["8473.TW", "8341.TW", "8422.TW"],
    "半導體先進封裝": ["2330.TW", "3711.TW", "2467.TW"],
    "PCB": ["4958.TW", "3037.TW", "3044.TW"],
    "散熱": ["3324.TWO", "3017.TW", "3653.TW"],
    "消費防禦": ["2912.TW", "1229.TW", "1210.TW"],
}

US_THEME_BASKETS = {
    "美股科技": ["XLK", "QQQ"],
    "美股半導體": ["SMH", "SOXX"],
    "美股公用事業/電力": ["XLU"],
    "美股消費防禦": ["XLP"],
    "美股工業": ["XLI"],
    "美股小型股": ["IWM"],
    "美股投機成長": ["ARKK"],
}

TOPIC_KEYWORDS = {
    "AI/GPU/資料中心": ["ai", "gpu", "h100", "h200", "gb200", "blackwell", "data center", "datacenter", "nvda", "nvidia", "server", "inference"],
    "半導體先進封裝": ["semiconductor", "tsmc", "advanced packaging", "chiplet", "cowos", "hybrid bonding", "glass substrate", "substrate"],
    "電力/核能/電網": ["power grid", "electricity", "utility", "nuclear", "uranium", "transformer", "data center power"],
    "降息/美債/信用": ["rate cut", "treasury", "yield", "credit spread", "hyg", "lqd", "recession", "liquidity"],
    "泡沫/崩盤/恐慌": ["bubble", "crash", "panic", "selloff", "bear market", "margin call"],
    "機器人/自動化": ["robot", "robotics", "automation", "humanoid", "tesla robot"],
    "低軌衛星/太空": ["satellite", "starlink", "leo", "space", "rocket", "rklb"],
}

REDDIT_SUBS = [
    "stocks", "investing", "wallstreetbets", "StockMarket", "SecurityAnalysis", "ValueInvesting",
    "Semiconductors", "technology", "artificial", "MachineLearning",
]


def fetch_json(url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[dict]:
    headers = {"User-Agent": "GlobalMarketRadarV11/1.1 by ChatGPT user automation"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def fetch_text(url: str, params: Optional[dict] = None, timeout: int = 12) -> str:
    headers = {"User-Agent": "GlobalMarketRadarV11/1.1"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


def theme_strength(df: pd.DataFrame, tickers: List[str]) -> Dict[str, object]:
    rows = []
    for t in tickers:
        s = series(df, "Close", t)
        v = series(df, "Volume", t)
        if len(s) < 65:
            continue
        r5, r20, r60 = ret(s, 5), ret(s, 20), ret(s, 60)
        above20 = s.iloc[-1] > ma(s, 20) if not pd.isna(ma(s,20)) else False
        above60 = s.iloc[-1] > ma(s, 60) if not pd.isna(ma(s,60)) else False
        vol_ratio = np.nan
        if len(v) >= 25 and v.tail(20).mean() > 0:
            vol_ratio = float(v.tail(5).mean() / v.tail(20).mean())
        # 勢能分數：偏向輪動偵測，不是估值報告
        score = 50
        if not pd.isna(r5): score += min(max(r5 * 140, -12), 16)
        if not pd.isna(r20): score += min(max(r20 * 110, -18), 22)
        if not pd.isna(r60): score += min(max(r60 * 55, -18), 24)
        if above20: score += 6
        if above60: score += 6
        if not pd.isna(vol_ratio) and vol_ratio > 1.25: score += 6
        rows.append({"ticker": t, "name": TICKERS.get(t,t), "r5": r5, "r20": r20, "r60": r60, "above20": above20, "vol": vol_ratio, "score": score})
    if not rows:
        return {"score": 0, "status": "資料不足", "leaders": [], "rows": []}
    avg = float(np.nanmean([x["score"] for x in rows]))
    if avg >= 82:
        status = "資金明顯青睞"
    elif avg >= 68:
        status = "轉強 / 輪動中"
    elif avg >= 55:
        status = "普通 / 觀察"
    else:
        status = "偏弱 / 尚未被青睞"
    leaders = sorted(rows, key=lambda x: x["score"], reverse=True)[:3]
    return {"score": avg, "status": status, "leaders": leaders, "rows": rows}


def build_industry_message(df: pd.DataFrame) -> str:
    now = TODAY.strftime("%Y-%m-%d %H:%M")
    lines: List[str] = []
    lines.append("🏭 全球市場雷達 v13.5｜產業輪動雷達")
    lines.append(f"時間：{now}（台北）")
    lines.append("")
    all_themes = []
    for name, basket in THEME_BASKETS.items():
        item = theme_strength(df, basket)
        all_themes.append((name, item))
    for name, basket in US_THEME_BASKETS.items():
        item = theme_strength(df, basket)
        all_themes.append((name, item))
    all_themes.sort(key=lambda x: x[1].get("score", 0), reverse=True)

    lines.append("目前較被資金青睞：")
    for name, item in all_themes[:7]:
        leaders = "、".join([f"{x['name']}" for x in item.get("leaders", [])]) or "無"
        lines.append(f"• {name}｜{item['score']:.0f}分｜{item['status']}｜領先：{leaders}")
    lines.append("")

    weak = [x for x in all_themes if x[1].get("score",0) < 55]
    lines.append("偏弱 / 尚未被青睞：")
    if weak:
        for name, item in weak[:5]:
            lines.append(f"• {name}｜{item['score']:.0f}分｜{item['status']}")
    else:
        lines.append("• 暫無明顯弱勢主題，但這不代表可以亂追高。")
    lines.append("")

    # 局部警報：商品、油氣、NASDAQ、Russell、銅鋁黃金
    lines.append("⚠️ 局部警報")
    local_alerts = []
    checks = [
        ("NASDAQ / QQQ", "QQQ"), ("Russell 2000", "IWM"), ("WTI原油", "CL=F"),
        ("黃金", "GLD"), ("美元", "UUP"), ("公用事業", "XLU"), ("消費防禦", "XLP"),
        ("投機成長", "ARKK"),
    ]
    for label, t in checks:
        s = series(df, "Close", t)
        if len(s) < 240:
            continue
        dist240 = s.iloc[-1] / ma(s, 240) - 1 if not pd.isna(ma(s,240)) else np.nan
        r5, r20 = ret(s,5), ret(s,20)
        if not pd.isna(dist240) and dist240 > 0.18:
            local_alerts.append(f"🟡 {label}：高於240日線 {pct(dist240)}，偏熱")
        if not pd.isna(r5) and abs(r5) > 0.08:
            local_alerts.append(f"🟡 {label}：5日變化 {pct(r5)}，短線波動異常")
        if not pd.isna(r20) and abs(r20) > 0.14:
            local_alerts.append(f"🟡 {label}：20日變化 {pct(r20)}，波段趨勢異常")
    if local_alerts:
        lines.extend(local_alerts[:12])
    else:
        lines.append("• 沒有明顯局部警報。")
    lines.append("")

    lines.append("🛰 衛星持股啟動提醒")
    lines.append("• 產業輪動分數高，只代表資金偏好，不代表便宜。")
    lines.append("• 若總控雷達仍是 452，可觀察強勢主題；若進入 514，衛星倉要縮手，等 R 模式；若進入 433，代表反攻條件初步成立。")
    lines.append("• 這是輪動雷達，不是買賣建議。雷達會告訴你哪裡有熱源，但不保證那不是烤肉架。")
    return "\n".join(lines)


def fetch_reddit_items(limit_per_sub: int = 8) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for sub in REDDIT_SUBS:
        url = f"https://www.reddit.com/r/{sub}/hot.json"
        js = fetch_json(url, params={"limit": limit_per_sub}, timeout=10)
        if not js:
            continue
        children = (((js.get("data") or {}).get("children")) or [])
        for c in children:
            d = c.get("data") or {}
            title = d.get("title") or ""
            if title:
                items.append({"source": f"Reddit r/{sub}", "title": title[:180]})
        time.sleep(0.25)
    return items


def fetch_hn_items(limit: int = 25) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    js = fetch_json("https://hn.algolia.com/api/v1/search_by_date", params={"tags":"story", "hitsPerPage": limit}, timeout=10)
    if not js:
        return items
    for h in js.get("hits", [])[:limit]:
        title = h.get("title") or h.get("story_title") or ""
        if title:
            items.append({"source": "Hacker News", "title": title[:180]})
    return items


def fetch_google_news_items() -> List[Dict[str, str]]:
    import xml.etree.ElementTree as ET
    queries = [
        "AI GPU data center semiconductor", "advanced packaging chiplet glass substrate",
        "power grid nuclear data center", "stock market bubble credit spread recession",
    ]
    items: List[Dict[str, str]] = []
    for q in queries:
        url = "https://news.google.com/rss/search"
        text = fetch_text(url, params={"q": q, "hl":"en-US", "gl":"US", "ceid":"US:en"}, timeout=10)
        if not text:
            continue
        try:
            root = ET.fromstring(text)
            for item in root.findall(".//item")[:6]:
                title = item.findtext("title") or ""
                if title:
                    items.append({"source": "Google News", "title": title[:180]})
        except Exception:
            pass
        time.sleep(0.15)
    return items


def score_topics(items: List[Dict[str, str]]) -> List[Tuple[str, int, List[str]]]:
    scored = []
    for topic, kws in TOPIC_KEYWORDS.items():
        hits = []
        score = 0
        for item in items:
            title_l = item["title"].lower()
            matched = [kw for kw in kws if kw.lower() in title_l]
            if matched:
                score += min(6, len(matched) * 2)
                hits.append(f"[{item['source']}] {item['title']}")
        scored.append((topic, score, hits[:7]))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def build_topic_message() -> str:
    now = TODAY.strftime("%Y-%m-%d %H:%M")
    reddit = fetch_reddit_items()
    hn = fetch_hn_items()
    google = fetch_google_news_items()
    items = reddit + hn + google
    scored = score_topics(items)

    lines: List[str] = []
    lines.append("🧭 全球市場雷達 v13.5｜Reddit / Hacker News / Google News 題材雷達")
    lines.append(f"時間：{now}（台北）")
    lines.append("")
    if not items:
        lines.append("這次沒有抓到 Reddit / News 資料，可能是來源暫時擋機器人或網路不穩。總控與產業輪動仍可正常使用。")
        return "\n".join(lines)

    lines.append("熱門題材分數：")
    for topic, score, hits in scored[:7]:
        if score <= 0:
            continue
        lines.append(f"• {topic}｜{score}分")
    lines.append("")

    for topic, score, hits in scored[:5]:
        if score <= 0 or not hits:
            continue
        lines.append(f"🧩 {topic}")
        lines.append(f"總分：{score}｜觸發關鍵字：{', '.join(TOPIC_KEYWORDS[topic][:4])}")
        for h in hits[:6]:
            lines.append(f"• {h}")
        lines.append("")

    lines.append("判讀提醒：")
    lines.append("• Reddit / HN / Google News 是市場耳語雷達，不是財報，也不是估值。")
    lines.append("• 它適合用來發現新題材、群眾焦點與泡沫溫度；真正下單仍要回到總控分數、產業輪動、公司基本面。")
    lines.append("• 如果 Reddit 很熱、總控卻進入 514，通常不是追高訊號，而是提醒你別站到人群最後面；若 433 成立，仍要分批。")
    return "\n".join(lines)


# ------------------------------------------------------------
# V13.4 亞洲槓桿壓力提醒 beta
# ------------------------------------------------------------
def fetch_asia_leverage_news_items() -> List[Dict[str, str]]:
    """News proxy only. This module is a reminder, not a trading signal."""
    import xml.etree.ElementTree as ET
    queries = [
        "Taiwan margin financing balance surged default settlement",
        "Taiwan failed settlement margin trading retail leverage",
        "Korea margin debt KOSPI retail leverage",
        "Korea leveraged ETF margin debt retail investors",
    ]
    items: List[Dict[str, str]] = []
    for q in queries:
        try:
            text = fetch_text(
                "https://news.google.com/rss/search",
                params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                timeout=10,
            )
            if not text:
                continue
            root = ET.fromstring(text)
            for item in root.findall(".//item")[:5]:
                title = item.findtext("title") or ""
                if title:
                    items.append({"source": "Google News", "title": title[:180], "query": q})
        except Exception:
            continue
        time.sleep(0.15)
    return items


def score_asia_leverage_reminder(df: pd.DataFrame, margin_df: pd.DataFrame) -> RadarResult:
    """Reminder-only radar for Taiwan / Asia leverage heat.

    This is intentionally NOT included in the weighted total score and must not
    block Data Health. It only reminds the user to watch leverage excess.
    """
    score = 0
    max_score = 100
    signals: List[str] = []
    notes: List[str] = []

    # 1) TWSE margin long-window change if local history is long enough.
    try:
        if margin_df is not None and not margin_df.empty and "margin_balance" in margin_df.columns:
            mdf = margin_df.copy()
            mdf["margin_balance"] = pd.to_numeric(mdf["margin_balance"], errors="coerce")
            mdf = mdf.dropna(subset=["margin_balance"])
            if "date" in mdf.columns:
                mdf["date_dt"] = pd.to_datetime(mdf["date"].astype(str), errors="coerce")
                mdf = mdf.dropna(subset=["date_dt"]).sort_values("date_dt")
            else:
                mdf = mdf.reset_index(drop=True)
                mdf["date_dt"] = pd.NaT

            if len(mdf) >= 2:
                latest = float(mdf["margin_balance"].iloc[-1])
                first = float(mdf["margin_balance"].iloc[0])
                chg_all = latest / first - 1 if first else np.nan
                notes.append(f"台股融資本地累積：{len(mdf)}筆；累積變化 {pct(chg_all)}。")

            if len(mdf) >= 200:
                latest = float(mdf["margin_balance"].iloc[-1])
                # Use about one trading year if exact 12M not available.
                base = float(mdf["margin_balance"].iloc[-200])
                chg12m = latest / base - 1 if base else np.nan
                notes.append(f"台股融資約12M/200交易日變化：{pct(chg12m)}。")
                if not pd.isna(chg12m):
                    if chg12m > 1.50:
                        score += add_signal(signals, 15, "台股融資12M暴增", f"約 {pct(chg12m)}")
                    elif chg12m > 1.00:
                        score += add_signal(signals, 10, "台股融資12M大幅增加", f"約 {pct(chg12m)}")
                    elif chg12m > 0.50:
                        score += add_signal(signals, 6, "台股融資12M升溫", f"約 {pct(chg12m)}")
            else:
                notes.append("台股融資本地資料尚未滿約200筆，暫時無法計算12M增幅；先用短期融資雷達與新聞提醒觀察。")

            if len(mdf) >= 5:
                latest = float(mdf["margin_balance"].iloc[-1])
                base5 = float(mdf["margin_balance"].iloc[-5])
                chg5 = latest / base5 - 1 if base5 else np.nan
                if not pd.isna(chg5) and chg5 > 0.05:
                    score += add_signal(signals, 8, "台股融資短期快速升溫", f"近5筆 {pct(chg5)}")
        else:
            notes.append("台股融資資料不足，亞洲槓桿提醒只看新聞 proxy。")
    except Exception as e:
        notes.append(f"台股融資12M提醒計算失敗：{type(e).__name__}")

    # 2) Taiwan weighted index near high + margin rising = heat reminder
    try:
        twii = series(df, "Close", "^TWII")
        if len(twii) >= 240 and margin_df is not None and not margin_df.empty:
            dist_high = twii.iloc[-1] / twii.rolling(240).max().iloc[-1] - 1
            if dist_high > -0.05:
                notes.append(f"加權指數距一年高點：{pct(dist_high)}。")
                # only mild reminder; actual leverage is handled by margin radar
                score += add_signal(signals, 4, "台股接近高檔", f"距一年高點 {pct(dist_high)}")
    except Exception:
        pass

    # 3) Failed settlement / Korea margin news proxy.
    try:
        items = fetch_asia_leverage_news_items()
        blob = " ".join([x.get("title", "") for x in items]).lower()
        taiwan_words = ["taiwan", "twse", "margin", "settlement", "default", "failed settlement", "retail leverage"]
        korea_words = ["korea", "kospi", "margin debt", "leveraged etf", "retail investors", "guarantee debt"]
        stress_words = ["surge", "record", "default", "failed", "plunge", "risk", "warning", "leverage"]

        tw_hits = sum(1 for w in taiwan_words if w in blob)
        kr_hits = sum(1 for w in korea_words if w in blob)
        stress_hits = sum(1 for w in stress_words if w in blob)

        if items:
            notes.append(f"亞洲槓桿新聞 proxy：Taiwan hits={tw_hits} / Korea hits={kr_hits} / stress hits={stress_hits}。")
        if tw_hits >= 3 and stress_hits >= 2:
            score += add_signal(signals, 8, "台灣槓桿/違約交割新聞升溫", "新聞 proxy 提醒")
        elif tw_hits >= 2 and stress_hits >= 1:
            score += add_signal(signals, 4, "台灣槓桿新聞觀察", "新聞 proxy 提醒")

        if kr_hits >= 3 and stress_hits >= 2:
            score += add_signal(signals, 8, "韓國槓桿新聞升溫", "亞洲散戶槓桿共振 proxy")
        elif kr_hits >= 2 and stress_hits >= 1:
            score += add_signal(signals, 4, "韓國槓桿新聞觀察", "新聞 proxy 提醒")

        if items:
            for item in items[:5]:
                title = item.get("title", "")
                if title:
                    notes.append(f"新聞觀察：[{item.get('source','News')}] {title}")
    except Exception:
        notes.append("亞洲槓桿新聞 proxy 暫時無法讀取；不影響主控。")

    if not signals:
        signals.append("無明顯亞洲槓桿壓力提醒")

    notes.append("本模組為提醒 beta：不納入加權總分、不阻止模式切換；只提醒台灣融資12M、違約交割新聞、韓國槓桿共振等泡沫後段訊號。")
    return RadarResult("亞洲槓桿壓力提醒 beta", min(score, max_score), max_score, signals, notes)

def main() -> None:
    try:
        df = download_market_data()
        if df is None or df.empty:
            raise RuntimeError("yfinance 沒有回傳資料，可能是網路或 Yahoo Finance 暫時異常。")

        macro = score_macro_pressure(df)
        credit = score_credit(df)
        market = score_market(df)
        fed = score_fed_liquidity(df)
        private_credit = score_private_credit_stress(df)
        breadth = score_breadth_greed(df)
        margin, margin_df = score_taiwan_margin(df)
        asia_leverage = score_asia_leverage_reminder(df, margin_df)
        results = [macro, credit, market, fed, private_credit, margin, breadth, asia_leverage]

        # V13 總控權重：加入 Fed 流動性硬數據，但不讓單一模組主導。
        # 總分權重：宏觀 20%、信用 20%、市場動能 20%、Fed流動性 10%、私募信貸 10%、融資 10%、廣度/貪婪 10%。
        total_risk = (
            macro.risk_pct * 0.20
            + credit.risk_pct * 0.20
            + market.risk_pct * 0.20
            + fed.risk_pct * 0.10
            + private_credit.risk_pct * 0.10
            + margin.risk_pct * 0.10
            + breadth.risk_pct * 0.10
        )
        total_risk = min(100.0, max(0.0, total_risk))

        raw_mode, raw_stance, reasons, next_watch, rebound = decide_battle_mode_v11(
            total_risk, macro, credit, market, margin, breadth, df
        )

        data_health_ok, data_health_lines, data_health_warnings, data_health_status = data_health_check(df, margin_df)

        os31_state = load_os31_state()
        mode, stance, os_reasons, new_os31_state = apply_os311_state_machine(
            raw_mode, raw_stance, os31_state,
            data_health_ok=data_health_ok,
            data_health_warnings=data_health_warnings,
            trend_lines=trend_lines,
        )
        reasons = [f"V13.5 原始訊號：{raw_mode}。"] + os_reasons + reasons
        save_os31_state(new_os31_state)

        # V13.5：可回補市場資料 + 雷達分數歷史累積
        write_market_backfill_90d(df)
        trend_lines = append_radar_history(total_risk, mode, raw_mode, data_health_ok, results, df, margin_df)

        # 第一則：全球總控
        msg1 = format_result(
            results, total_risk, mode, stance, reasons, next_watch, rebound, df, margin_df,
            raw_mode=raw_mode,
            os_state=new_os31_state,
            data_health_lines=data_health_lines,
            data_health_warnings=data_health_warnings,
        )
        # 第二則：產業輪動
        msg2 = build_industry_message(df)
        # 第三則：Reddit / HN / Google News 題材
        msg3 = build_topic_message()

        print(msg1)
        print("\n" + "="*80 + "\n")
        print(msg2)
        print("\n" + "="*80 + "\n")
        print(msg3)

        send_telegram(msg1)
        time.sleep(1.2)
        send_telegram(msg2)
        time.sleep(1.2)
        send_telegram(msg3)
    except Exception as e:
        err = "🚨 全球市場雷達 v13.5 執行失敗\n" + str(e) + "\n\n" + traceback.format_exc()
        print(err)
        send_telegram(err[:3800])
        raise


# ------------------------------------------------------------
# V13.1 FRED hardened fetcher override
# ------------------------------------------------------------
def fetch_fred_series(series_id: str) -> pd.Series:
    """Fetch FRED series with multiple fallbacks.

    Priority:
    1) Official FRED API if FRED_API_KEY is provided in GitHub Secrets.
    2) FRED public fredgraph CSV.
    3) FRED CSV through a simple raw proxy as last resort.

    If all fail, return empty series and let the Fed liquidity module degrade gracefully.
    """
    from io import StringIO
    import urllib.parse

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 GlobalMarketRadarV13.5",
        "Accept": "application/json,text/csv,text/plain,*/*",
        "Cache-Control": "no-cache",
    }

    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if api_key:
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "observation_start": "2018-01-01",
                },
                headers=headers,
                timeout=25,
            )
            if r.status_code == 200:
                obs = r.json().get("observations", [])
                rows = []
                for x in obs:
                    v = x.get("value")
                    if v in (None, "", "."):
                        continue
                    d = pd.to_datetime(x.get("date"), errors="coerce")
                    val = safe_float(v)
                    if not pd.isna(d) and not pd.isna(val):
                        rows.append((d, val))
                if rows:
                    s = pd.Series([v for _, v in rows], index=[d for d, _ in rows]).dropna().astype(float)
                    if len(s):
                        return s
        except Exception:
            pass

    urls = [
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?cosd=2018-01-01&id={series_id}",
    ]
    for url in urls:
        for _ in range(2):
            try:
                r = requests.get(url, headers=headers, timeout=25)
                if r.status_code != 200 or not r.text.strip():
                    time.sleep(0.8)
                    continue
                df = pd.read_csv(StringIO(r.text))
                if "DATE" not in df.columns or series_id not in df.columns:
                    time.sleep(0.8)
                    continue
                df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
                vals = pd.to_numeric(df[series_id].replace(".", np.nan), errors="coerce")
                s = pd.Series(vals.values, index=df["DATE"]).dropna().astype(float)
                if len(s):
                    return s
            except Exception:
                time.sleep(0.8)
                continue

    # Last resort proxy: relays official FRED CSV only. Not used as source of truth.
    try:
        base_url = urls[0]
        purl = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(base_url, safe="")
        r = requests.get(purl, headers=headers, timeout=30)
        if r.status_code == 200 and r.text.strip():
            df = pd.read_csv(StringIO(r.text))
            if "DATE" in df.columns and series_id in df.columns:
                df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
                vals = pd.to_numeric(df[series_id].replace(".", np.nan), errors="coerce")
                s = pd.Series(vals.values, index=df["DATE"]).dropna().astype(float)
                if len(s):
                    return s
    except Exception:
        pass

    return pd.Series(dtype=float)

def _v13_save_snapshot():
    try:
        import json
        from datetime import datetime
        from zoneinfo import ZoneInfo
        snap = {
            "version": "v13.5-mobile-flat",
            "time_taipei": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds"),
            "state_file": STATE_FILE,
            "tw_margin_history_file": MARGIN_HISTORY_FILE,
            "tpex_margin_history_file": TPEX_MARGIN_HISTORY_FILE,
        }
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                snap["os31_state"] = json.load(f)
        except Exception:
            snap["os31_state"] = {}
        os.makedirs("storage", exist_ok=True)
        with open("storage/last_radar_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        with open("storage/source_status.json", "w", encoding="utf-8") as f:
            json.dump({"engine": {"status": "ok", "version": "v13.5-mobile-flat"}}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("V13.4 snapshot save failed:", e)

if __name__ == "__main__":
    try:
        main()
        _v13_save_snapshot()
    except Exception as e:
        try:
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if token and chat_id:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": f"❌ 全球市場雷達 v13.5 執行失敗\n\n錯誤：{type(e).__name__}: {e}\n\n請到 GitHub Actions 查看 log。"},
                    timeout=15,
                )
        except Exception:
            pass
        raise
