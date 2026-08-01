#!/usr/bin/env python3
"""
Yahoo Finance data fetcher - Simplified version matching the old working scanner.
Uses pure yfinance with sequential batch processing (old working approach).
Cross-platform timeout using threading.
"""
import json
import math
import random
import signal
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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


def is_rate_limit_error(err: Exception) -> bool:
    """Check if error is a 429 rate limit error."""
    err_str = str(err).lower()
    return "429" in err_str or "too many requests" in err_str or "rate limit" in err_str


def append_log(stderr_path: str, message: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(stderr_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def run_with_hard_timeout(seconds, fn):
    """Cross-platform timeout using ThreadPoolExecutor."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=seconds)
        except FuturesTimeoutError:
            raise TimeoutError(f"hard timeout after {seconds}s")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def download_bars(
    symbols: list[str],
    period: str,
    stderr_path: str,
    batch: int = 200,
    phase: str = "DOWNLOAD",
    interval: str = "1d",
) -> tuple[dict[str, pd.DataFrame], set[str]]:
    """
    Download daily bars using yfinance batch mode (old working approach).
    Sequential batch processing with simple retry logic.
    """
    frames = []
    misses = set()
    
    total_batches = max(1, math.ceil(len(symbols) / batch)) if symbols else 0
    for group_idx, group in enumerate(chunked(symbols, batch), start=1):
        batch_start = time.time()
        append_log(
            stderr_path,
            f"{phase}_BATCH_START period={period} batch={group_idx}/{total_batches} size={len(group)} accumulated_ok={len(frames)} accumulated_miss={len(misses)}"
        )
        tickers = ' '.join(group)
        time.sleep(0.35 + random.uniform(0.0, 0.55))
        data = None
        last_error = None
        for attempt in range(1, 4):
            try:
                data = run_with_hard_timeout(
                    60,  # Increased timeout
                    lambda: yf.download(
                        tickers=tickers,
                        period=period,
                        interval=interval,
                        auto_adjust=False,
                        group_by='ticker',
                        progress=False,
                        threads=False,
                        prepost=False,
                        timeout=30,
                    )
                )
                if data is not None and len(data) != 0:
                    # Debug: log what we got
                    append_log(stderr_path, f"{phase}_DEBUG got data shape={getattr(data, 'shape', 'N/A')} columns={list(data.columns)[:5]}")
                    break
                last_error = RuntimeError('empty download result')
            except Exception as e:
                last_error = e
                append_log(stderr_path, f"{phase}_DEBUG attempt={attempt} exception={type(e).__name__}: {e}")
            wait_s = 0.8 * attempt + random.uniform(0.6, 1.8)
            append_log(
                stderr_path,
                f"{phase}_RETRY period={period} batch={group_idx}/{total_batches} attempt={attempt} size={len(group)} wait={wait_s:.2f}s error={last_error}"
            )
            if attempt < 3:
                time.sleep(wait_s)
        if data is None or len(data) == 0:
            append_log(
                stderr_path,
                f"{phase}_ERROR period={period} batch={group_idx}/{total_batches} sample={group[:5]} error={last_error}"
            )
            misses.update(group)
            continue
        before_frames = len(frames)
        before_misses = len(misses)
        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.nlevels == 2:
                if data.columns[0][0] in ["Adj Close", "Close", "High", "Low", "Open", "Volume"]:
                    # single ticker shape from yfinance sometimes
                    if len(group) == 1:
                        sym = group[0]
                        df = data.copy()
                        df.columns = [c[0] for c in df.columns]
                        df = df.reset_index().rename(columns={df.index.name or 'Date': 'Date'})
                        frames.append((sym, df))
                    else:
                        # unexpected; try extract by top-level names if possible
                        for sym in group:
                            try:
                                sdf = data[sym].reset_index()
                                frames.append((sym, sdf))
                            except Exception:
                                misses.add(sym)
                    continue
                for sym in group:
                    try:
                        sdf = data[sym].copy().reset_index()
                        if len(sdf.dropna(how='all')) == 0:
                            misses.add(sym)
                        else:
                            frames.append((sym, sdf))
                    except Exception:
                        misses.add(sym)
            else:
                misses.update(group)
        else:
            if len(group) == 1:
                sdf = data.reset_index()
                frames.append((group[0], sdf))
            else:
                misses.update(group)
        batch_ok = len(frames) - before_frames
        batch_miss = len(misses) - before_misses
        elapsed = time.time() - batch_start
        append_log(
            stderr_path,
            f"{phase}_BATCH_DONE period={period} batch={group_idx}/{total_batches} size={len(group)} ok={batch_ok} miss={batch_miss} cumulative_ok={len(frames)} cumulative_miss={len(misses)} elapsed={elapsed:.2f}s"
        )
        time.sleep(0.15)
    out = {}
    for sym, df in frames:
        cols = {c.lower(): c for c in df.columns}
        needed = [cols.get('date'), cols.get('open'), cols.get('high'), cols.get('low'), cols.get('close'), cols.get('volume')]
        if any(c is None for c in needed):
            misses.add(sym)
            continue
        sdf = df[[cols['date'], cols['open'], cols['high'], cols['low'], cols['close'], cols['volume']]].copy()
        sdf.columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
        sdf = sdf.dropna(subset=['Date']).sort_values('Date')
        if len(sdf) == 0 or sdf[['Open','High','Low','Close']].dropna(how='all').empty:
            misses.add(sym)
            continue
        sdf['Date'] = pd.to_datetime(sdf['Date']).dt.tz_localize(None)
        out[sym] = sdf.reset_index(drop=True)
    return out, misses


if __name__ == "__main__":
    # Quick test
    import sys
    logging_path = "/tmp/test_yahoo.log"
    Path(logging_path).write_text("")
    
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    df = download_bars([sym], "5y", logging_path)
    print(f"Fetched {len(df)} rows for {sym}")
    if df:
        print(df[0][1].head())
        print(df[0][1].tail())
