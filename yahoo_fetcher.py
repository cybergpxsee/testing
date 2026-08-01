#!/usr/bin/env python3
"""
Yahoo Finance data fetcher with multiple backends and robust rate limiting.
Supports both yfinance (primary) and curl_cffi (fallback).
"""
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

# Import yfinance as primary backend
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# Import curl_cffi as alternative backend
try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


INTERVAL_MAP = {
    "1d": "1d",
    "1wk": "1wk",
    "1mo": "1mo",
}

PERIOD_MAP = {
    "1d": "1d",
    "5d": "5d",
    "1mo": "1mo",
    "3mo": "3mo",
    "6mo": "6mo",
    "1y": "1y",
    "2y": "2y",
    "5y": "5y",
    "10y": "10y",
    "ytd": "ytd",
    "max": "max",
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def is_rate_limit_error(err: Exception) -> bool:
    """Check if error is a 429 rate limit error."""
    err_str = str(err).lower()
    return "429" in err_str or "too many requests" in err_str or "rate limit" in err_str


def append_log(stderr_path: str, message: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(stderr_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def fetch_yahoo_chart_curl(
    symbol: str,
    period: str = "5y",
    interval: str = "1wk",
    timeout: int = 30,
    max_retries: int = 5,
    stderr_path: Optional[str] = None,
    phase: str = "DOWNLOAD",
) -> Optional[pd.DataFrame]:
    """
    Fetch chart data using curl_cffi with Chrome impersonation.
    """
    if not CURL_CFFI_AVAILABLE:
        return None
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval": INTERVAL_MAP.get(interval, "1wk"),
        "range": PERIOD_MAP.get(period, "5y"),
        "includePrePost": "false",
        "events": "div,splits",
    }
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://finance.yahoo.com/quote/{symbol}",
        "Origin": "https://finance.yahoo.com",
    })
    
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            session = cffi_requests.Session(impersonate="chrome120")
            
            # First visit the quote page to get cookies
            quote_resp = session.get(f"https://finance.yahoo.com/quote/{symbol}", headers=headers, timeout=timeout)
            if quote_resp.status_code == 403:
                raise RuntimeError("403 Forbidden on quote page")
            
            # Now fetch chart data
            resp = session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            
            if resp.status_code == 429:
                raise RuntimeError(f"429 Too Many Requests: {resp.text[:200]}")
            elif resp.status_code == 404:
                raise RuntimeError(f"404 Not Found: {symbol}")
            elif resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            
            data = resp.json()
            
            if not data.get("chart", {}).get("result"):
                if stderr_path:
                    append_log(stderr_path, f"{phase}_EMPTY_RESULT symbol={symbol}")
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
            if is_rate_limit_error(e):
                wait_s = 60 * (2 ** (attempt - 1)) + random.uniform(0, 30)
                if stderr_path:
                    append_log(stderr_path, f"{phase}_RATE_LIMIT symbol={symbol} attempt={attempt}/{max_retries} wait={wait_s:.0f}s err={e}")
            else:
                wait_s = 10 * attempt + random.uniform(5, 15)
                if stderr_path:
                    append_log(stderr_path, f"{phase}_RETRY symbol={symbol} attempt={attempt}/{max_retries} wait={wait_s:.1f}s err={e}")
            
            if attempt < max_retries:
                time.sleep(wait_s)
    
    if stderr_path:
        append_log(stderr_path, f"{phase}_ERROR symbol={symbol} err={last_err}")
    return None


def fetch_yahoo_chart_yfinance(
    symbol: str,
    period: str = "5y",
    interval: str = "1wk",
    timeout: int = 30,
    max_retries: int = 3,
    stderr_path: Optional[str] = None,
    phase: str = "DOWNLOAD",
) -> Optional[pd.DataFrame]:
    """
    Fetch chart data using yfinance (primary backend).
    """
    if not YFINANCE_AVAILABLE:
        return None
    
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
    
    def run_with_timeout(seconds, fn):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fn)
            try:
                return future.result(timeout=seconds)
            except FuturesTimeoutError:
                raise TimeoutError(f"hard timeout after {seconds}s")
    
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            data = run_with_timeout(45, lambda: yf.download(
                tickers=symbol, period=period, interval=interval,
                auto_adjust=False, group_by="ticker", progress=False,
                threads=False, prepost=False, timeout=timeout
            ))
            if data is not None and len(data) > 0:
                if isinstance(data.columns, pd.MultiIndex) and data.columns.nlevels == 2:
                    sdf = data.xs(symbol, level=1, axis=1).copy()
                else:
                    sdf = data.copy()
                sdf = sdf.reset_index().rename(columns={sdf.index.name or "Date": "Date"})
                return sdf
            last_err = RuntimeError("empty result")
        except Exception as e:
            last_err = e
        
        if is_rate_limit_error(last_err):
            wait_s = 60 * (2 ** (attempt - 1)) + random.uniform(0, 30)
            if stderr_path:
                append_log(stderr_path, f"{phase}_RATE_LIMIT symbol={symbol} attempt={attempt}/{max_retries} wait={wait_s:.0f}s err={last_err}")
        else:
            wait_s = 5 * attempt + random.uniform(2, 5)
            if stderr_path:
                append_log(stderr_path, f"{phase}_RETRY symbol={symbol} attempt={attempt}/{max_retries} wait={wait_s:.1f}s err={last_err}")
        
        if attempt < max_retries:
            time.sleep(wait_s)
    
    if stderr_path:
        append_log(stderr_path, f"{phase}_ERROR symbol={symbol} err={last_err}")
    return None


def fetch_yahoo_chart(
    symbol: str,
    period: str = "5y",
    interval: str = "1wk",
    timeout: int = 30,
    max_retries: int = 5,
    stderr_path: Optional[str] = None,
    phase: str = "DOWNLOAD",
    prefer_backend: str = "yfinance",  # "yfinance" or "curl_cffi"
) -> Optional[pd.DataFrame]:
    """
    Fetch chart data with automatic backend selection and fallback.
    
    Args:
        symbol: Yahoo Finance symbol (e.g., "AAPL", "MSFT")
        period: Lookback period (e.g., "5y", "2mo", "1y")
        interval: Data interval (e.g., "1wk", "1d", "1mo")
        timeout: Request timeout in seconds
        max_retries: Maximum retry attempts
        stderr_path: Path to log file for debugging
        phase: Phase label for logging
        prefer_backend: Preferred backend ("yfinance" or "curl_cffi")
    
    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume (and optionally Adj Close)
    """
    # Try preferred backend first
    if prefer_backend == "yfinance" and YFINANCE_AVAILABLE:
        df = fetch_yahoo_chart_yfinance(symbol, period, interval, timeout, max_retries, stderr_path, phase)
        if df is not None:
            return df
        # Fallback to curl_cffi
        if stderr_path:
            append_log(stderr_path, f"{phase}_FALLBACK_TO_CURL_CFFI symbol={symbol}")
    
    if prefer_backend == "curl_cffi" and CURL_CFFI_AVAILABLE:
        df = fetch_yahoo_chart_curl(symbol, period, interval, timeout, max_retries, stderr_path, phase)
        if df is not None:
            return df
        # Fallback to yfinance
        if stderr_path:
            append_log(stderr_path, f"{phase}_FALLBACK_TO_YFINANCE symbol={symbol}")
    
    # Try other backend as final fallback
    if prefer_backend != "curl_cffi" and CURL_CFFI_AVAILABLE:
        return fetch_yahoo_chart_curl(symbol, period, interval, timeout, max_retries, stderr_path, phase)
    elif prefer_backend != "yfinance" and YFINANCE_AVAILABLE:
        return fetch_yahoo_chart_yfinance(symbol, period, interval, timeout, max_retries, stderr_path, phase)
    
    return None


def download_bars(
    symbols: list[str],
    period: str,
    stderr_path: str,
    batch: int = 80,
    phase: str = "DOWNLOAD",
    interval: str = "1wk",
    prefer_backend: str = "curl_cffi",
) -> tuple[dict[str, pd.DataFrame], set[str]]:
    """
    Main entry point matching the original download_bars interface.
    
    For daily intervals with multiple symbols, uses yfinance batch mode.
    For weekly intervals, downloads sequentially with rate limiting.
    """
    import math
    
    all_frames = {}
    all_misses = set()
    
    if interval == "1d" and len(symbols) > 1 and YFINANCE_AVAILABLE:
        # Use yfinance batch mode for daily data
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def is_rate_limit_error(err):
            return is_rate_limit_error(err)
        
        total_batches = max(1, math.ceil(len(symbols) / batch))
        
        def download_one_batch(group_idx, group):
            batch_start = time.time()
            append_log(stderr_path, f"{phase}_BATCH_START period={period} batch={group_idx}/{total_batches} size={len(group)}")
            
            tickers_str = " ".join(group)
            data = None
            last_err = None
            
            for attempt in range(1, 8):
                try:
                    data = yf.download(
                        tickers=tickers_str, period=period, interval=interval,
                        auto_adjust=False, group_by="ticker", progress=False,
                        threads=True, prepost=False, timeout=90
                    )
                    if data is not None and len(data) > 0:
                        break
                    last_err = RuntimeError("empty result")
                except Exception as e:
                    last_err = e
                
                if is_rate_limit_error(last_err):
                    wait_s = 60 * (2 ** (attempt - 1)) + random.uniform(0, 30)
                    append_log(stderr_path, f"{phase}_RATE_LIMIT batch={group_idx} attempt={attempt}/7 wait={wait_s:.0f}s err={last_err}")
                else:
                    wait_s = 10 * attempt + random.uniform(5, 15)
                    append_log(stderr_path, f"{phase}_RETRY batch={group_idx} attempt={attempt}/7 wait={wait_s:.1f}s err={last_err}")
                
                if attempt < 7:
                    time.sleep(wait_s)
            
            local_frames = []
            local_misses = set()
            
            if data is None or len(data) == 0:
                append_log(stderr_path, f"{phase}_ERROR period={period} batch={group_idx} err={last_err}")
                local_misses.update(group)
            else:
                try:
                    if isinstance(data.columns, pd.MultiIndex) and data.columns.nlevels == 2:
                        for sym in group:
                            try:
                                sdf = data.xs(sym, level=1, axis=1).copy()
                                sdf = sdf.reset_index().rename(columns={sdf.index.name or "Date": "Date"})
                                local_frames.append((sym, sdf))
                            except Exception:
                                local_misses.add(sym)
                    else:
                        if len(group) == 1:
                            sdf = data.copy().reset_index().rename(columns={data.index.name or "Date": "Date"})
                            local_frames.append((group[0], sdf))
                        else:
                            local_misses.update(group)
                except Exception as e:
                    append_log(stderr_path, f"{phase}_PARSE_ERROR batch={group_idx} err={e}")
                    local_misses.update(group)
            
            elapsed = time.time() - batch_start
            append_log(stderr_path, f"{phase}_BATCH_DONE period={period} batch={group_idx}/{total_batches} ok={len(local_frames)} miss={len(local_misses)} elapsed={elapsed:.1f}s")
            return local_frames, local_misses
        
        # Split into batches
        group_list = [symbols[i:i+batch] for i in range(0, len(symbols), batch)]
        group_list = [(i+1, g) for i, g in enumerate(group_list)]
        
        # Multiple workers for speed, curl_cffi handles rate limiting
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for idx, g in group_list:
                future = executor.submit(download_one_batch, idx, g)
                futures.append(future)
            
            for future in as_completed(futures):
                local_frames, local_misses = future.result()
                for sym, df in local_frames:
                    # Normalize columns
                    cols = {c.lower(): c for c in df.columns}
                    needed = [cols.get("date"), cols.get("open"), cols.get("high"), cols.get("low"), cols.get("close"), cols.get("volume")]
                    if any(c is None for c in needed):
                        all_misses.add(sym)
                        continue
                    sdf = df[[cols["date"], cols["open"], cols["high"], cols["low"], cols["close"], cols["volume"]]].copy()
                    sdf.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
                    sdf = sdf.dropna(subset=["Date"]).sort_values("Date")
                    if len(sdf) == 0 or sdf[["Open","High","Low","Close"]].dropna(how="all").empty:
                        all_misses.add(sym)
                        continue
                    sdf["Date"] = pd.to_datetime(sdf["Date"]).dt.tz_localize(None)
                    all_frames[sym] = sdf.reset_index(drop=True)
                all_misses.update(local_misses)
        
        return all_frames, all_misses
    
    else:
        # Sequential download for weekly/monthly or single symbols
        total_batches = max(1, math.ceil(len(symbols) / batch)) if symbols else 0
        for group_idx, group in enumerate([symbols[i:i+batch] for i in range(0, len(symbols), batch)], 1):
            batch_start = time.time()
            append_log(stderr_path, f"{phase}_BATCH_START period={period} batch={group_idx}/{total_batches} size={len(group)}")
            
            for sym in group:
                time.sleep(0.1 + random.uniform(0, 0.1))
                df = fetch_yahoo_chart(sym, period, interval, stderr_path=stderr_path, phase=phase, prefer_backend=prefer_backend)
                if df is not None:
                    # Normalize columns
                    cols = {c.lower(): c for c in df.columns}
                    needed = [cols.get("date"), cols.get("open"), cols.get("high"), cols.get("low"), cols.get("close"), cols.get("volume")]
                    if any(c is None for c in needed):
                        all_misses.add(sym)
                        continue
                    sdf = df[[cols["date"], cols["open"], cols["high"], cols["low"], cols["close"], cols["volume"]]].copy()
                    sdf.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
                    sdf = sdf.dropna(subset=["Date"]).sort_values("Date")
                    if len(sdf) == 0 or sdf[["Open","High","Low","Close"]].dropna(how="all").empty:
                        all_misses.add(sym)
                        continue
                    sdf["Date"] = pd.to_datetime(sdf["Date"]).dt.tz_localize(None)
                    all_frames[sym] = sdf.reset_index(drop=True)
                else:
                    all_misses.add(sym)
            
            elapsed = time.time() - batch_start
            ok_count = len([f for f in all_frames if f in group])
            miss_count = len([m for m in all_misses if m in group])
            append_log(stderr_path, f"{phase}_BATCH_DONE period={period} batch={group_idx}/{total_batches} ok={ok_count} miss={miss_count} elapsed={elapsed:.1f}s")
        
        return all_frames, all_misses


def download_daily_bars(
    symbols: list[str],
    period: str,
    stderr_path: str,
    batch: int = 80,
    phase: str = "DOWNLOAD",
    prefer_backend: str = "curl_cffi",
) -> tuple[dict[str, pd.DataFrame], set[str]]:
    """Monthly update: download daily data with yfinance batch mode."""
    return download_bars(symbols, period, stderr_path, batch=batch, phase=phase, interval="1d", prefer_backend=prefer_backend)


if __name__ == "__main__":
    # Quick test
    import sys
    logging_path = "/tmp/test_yahoo.log"
    Path(logging_path).write_text("")
    
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    df = fetch_yahoo_chart(sym, "5y", "1wk", stderr_path=logging_path)
    print(f"Fetched {len(df)} rows for {sym}")
    if df is not None:
        print(df.head())
        print(df.tail())