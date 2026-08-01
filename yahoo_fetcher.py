# yahoo_fetcher.py
import random
import time
from datetime import datetime, timezone
from typing import Optional, Tuple, List
import pandas as pd

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

from logging_utils import log_info, log_warning, log_error

INTERVAL_MAP = {"1d": "1d", "1wk": "1wk", "1mo": "1mo"}
PERIOD_MAP = {
    "1d": "1d", "5d": "5d", "1mo": "1mo", "3mo": "3mo",
    "6mo": "6mo", "1y": "1y", "2y": "2y", "5y": "5y", "10y": "10y", "ytd": "ytd", "max": "max"
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

def _is_rate_limit(err):
    s = str(err).lower()
    return "429" in s or "too many requests" in s or "rate limit" in s

def fetch_yahoo_chart_curl(symbol: str, period: str, interval: str, timeout=30, max_retries=5) -> Optional[pd.DataFrame]:
    if not CURL_CFFI_AVAILABLE:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": INTERVAL_MAP.get(interval, "1wk"), "range": PERIOD_MAP.get(period, "5y"), "includePrePost": "false"}
    headers = DEFAULT_HEADERS.copy()
    headers["Referer"] = f"https://finance.yahoo.com/quote/{symbol}"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            session = cffi_requests.Session(impersonate="chrome120")
            # 先访问quote页面获取cookies
            session.get(f"https://finance.yahoo.com/quote/{symbol}", headers=headers, timeout=timeout)
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                raise RuntimeError(f"429 Too Many Requests")
            elif resp.status_code == 404:
                raise RuntimeError(f"404 Not Found: {symbol}")
            elif resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            data = resp.json()
            if not data.get("chart", {}).get("result"):
                return None
            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            adjclose = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", None)
            if not timestamps:
                return None
            df = pd.DataFrame({
                "Date": [datetime.fromtimestamp(ts, tz=timezone.utc) for ts in timestamps],
                "Open": quotes.get("open", []),
                "High": quotes.get("high", []),
                "Low": quotes.get("low", []),
                "Close": quotes.get("close", []),
                "Volume": quotes.get("volume", []),
            })
            if adjclose:
                df["Adj Close"] = adjclose
            df = df.dropna(subset=["Open", "High", "Low", "Close"], how="all")
            if len(df) == 0:
                return None
            return df.reset_index(drop=True)
        except Exception as e:
            last_err = e
            if _is_rate_limit(e):
                wait = 60 * (2 ** (attempt - 1)) + random.uniform(0, 30)
            else:
                wait = 10 * attempt + random.uniform(5, 15)
            if attempt < max_retries:
                time.sleep(wait)
    log_error(f"curl_cffi failed for {symbol}: {last_err}")
    return None

def fetch_yahoo_chart_yfinance(symbol: str, period: str, interval: str, timeout=30, max_retries=3) -> Optional[pd.DataFrame]:
    if not YFINANCE_AVAILABLE:
        return None
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            data = yf.download(
                tickers=symbol, period=period, interval=interval,
                auto_adjust=False, progress=False, threads=False, prepost=False, timeout=timeout
            )
            if data is not None and len(data) > 0:
                if isinstance(data.columns, pd.MultiIndex):
                    sdf = data.xs(symbol, level=1, axis=1).copy()
                else:
                    sdf = data.copy()
                sdf = sdf.reset_index().rename(columns={sdf.index.name or "Date": "Date"})
                return sdf
            last_err = RuntimeError("empty result")
        except Exception as e:
            last_err = e
        if _is_rate_limit(last_err):
            wait = 60 * (2 ** (attempt - 1)) + random.uniform(0, 30)
        else:
            wait = 5 * attempt + random.uniform(2, 5)
        if attempt < max_retries:
            time.sleep(wait)
    log_error(f"yfinance failed for {symbol}: {last_err}")
    return None

def fetch_yahoo_chart(symbol: str, period: str, interval: str = "1d", timeout=30, max_retries=5, prefer="yfinance") -> Optional[pd.DataFrame]:
    if prefer == "yfinance" and YFINANCE_AVAILABLE:
        df = fetch_yahoo_chart_yfinance(symbol, period, interval, timeout, max_retries)
        if df is not None:
            return df
    if CURL_CFFI_AVAILABLE:
        df = fetch_yahoo_chart_curl(symbol, period, interval, timeout, max_retries)
        if df is not None:
            return df
    if prefer != "yfinance" and YFINANCE_AVAILABLE:
        return fetch_yahoo_chart_yfinance(symbol, period, interval, timeout, max_retries)
    return None

def download_bars(symbols: List[str], period: str, stderr_path: str, batch: int = 80, phase: str = "DOWNLOAD", interval: str = "1d", prefer_backend: str = "yfinance") -> Tuple[dict, set]:
    """
    兼容原接口的下载函数，支持缓存。
    实际缓存由外层调用者（us_pattern_scan.download_bars）负责，此函数只负责原始下载。
    """
    from cache_utils import get_cache
    cache = get_cache()
    cached_frames = {}
    to_download = []
    for sym in symbols:
        cached = cache.get(sym, period)
        if cached is not None:
            cached_frames[sym] = cached
        else:
            to_download.append(sym)
    if not to_download:
        return cached_frames, set()

    # 批量下载（单股或多股）
    all_frames = {}
    misses = set()
    if interval == "1d" and len(to_download) > 1 and YFINANCE_AVAILABLE:
        # 使用yfinance批量
        import math
        total = len(to_download)
        batches = [to_download[i:i+batch] for i in range(0, total, batch)]
        for idx, group in enumerate(batches, 1):
            tickers = " ".join(group)
            data = None
            for attempt in range(1, 8):
                try:
                    data = yf.download(
                        tickers=tickers, period=period, interval=interval,
                        auto_adjust=False, group_by="ticker", progress=False,
                        threads=True, prepost=False, timeout=90
                    )
                    if data is not None and len(data) > 0:
                        break
                except Exception:
                    pass
                if attempt < 7:
                    time.sleep(10 * attempt + random.uniform(5, 15))
            if data is None or len(data) == 0:
                misses.update(group)
                continue
            if isinstance(data.columns, pd.MultiIndex):
                for sym in group:
                    try:
                        sdf = data.xs(sym, level=1, axis=1).copy()
                        sdf = sdf.reset_index().rename(columns={sdf.index.name or "Date": "Date"})
                        all_frames[sym] = sdf
                    except Exception:
                        misses.add(sym)
            else:
                if len(group) == 1:
                    sdf = data.reset_index().rename(columns={data.index.name or "Date": "Date"})
                    all_frames[group[0]] = sdf
                else:
                    misses.update(group)
    else:
        # 逐个下载
        for sym in to_download:
            time.sleep(0.2 + random.uniform(0, 0.2))
            df = fetch_yahoo_chart(sym, period, interval, prefer=prefer_backend)
            if df is not None:
                all_frames[sym] = df
            else:
                misses.add(sym)

    # 缓存新数据
    for sym, df in all_frames.items():
        cache.set(sym, period, df)
    # 合并缓存和新的
    all_frames.update(cached_frames)
    return all_frames, misses