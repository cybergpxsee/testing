#!/usr/bin/env python3
import argparse
import json
import math
import os
import random
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import yfinance as yf

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
UA = "Mozilla/5.0 (X11; Linux x86_64) Hermes-Agent/1.0"
SWING_WINDOW = 3
SHORT_TREND_LOOKBACK = 30
LONG_TREND_LOOKBACK = 90
LONG_TERM_TREND_BONUS = 5
LONG_SPAN_BONUS_PER_10D = 1   # 每 10 日加 1 分
LONG_SPAN_BONUS_MAX = 15       # 上限 15 分
DIRECTION_FILTER_DAYS = 5
DIRECTION_FILTER_MIN_PCT = 1.0

# 籌碼密集區參數
CHIP_LOOKBACK = 80             # 籌碼密集區看回 80 日

# 30m 信號參數
INTRADAY_30M_BONUS = 8         # 30m 信號加分降為 8 分
INTRADAY_30M_MAX_HOURS_AFTER = 36  # 回調日當日或次日上午 (約 36 小時內)

# 確認日燭線品質參數
CONFIRM_BODY_DIR_BONUS = 3
CONFIRM_BODY_SIZE_BONUS = 3
CONFIRM_VOLUME_BONUS = 3


def _hard_timeout_handler(signum, frame):
    raise TimeoutError('hard timeout waiting for yfinance download')


def run_with_hard_timeout(seconds, fn):
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _hard_timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_nasdaq_listed(text: str) -> pd.DataFrame:
    df = pd.read_csv(StringIO(text), sep="|")
    df = df[df["Symbol"].notna()]
    df = df[df["Symbol"] != "File Creation Time"]
    df["source"] = "nasdaq"
    df["name"] = df["Security Name"].fillna("")
    df["etf"] = df["ETF"].fillna("N").astype(str).str.upper().eq("Y")
    df["test_issue"] = df["Test Issue"].fillna("N").astype(str).str.upper().eq("Y")
    return df[["Symbol", "name", "etf", "test_issue", "source"]]


def parse_other_listed(text: str) -> pd.DataFrame:
    df = pd.read_csv(StringIO(text), sep="|")
    df = df[df["ACT Symbol"].notna()]
    df = df[df["ACT Symbol"] != "File Creation Time"]
    df["source"] = "other"
    df["name"] = df["Security Name"].fillna("")
    df["etf"] = df["ETF"].fillna("N").astype(str).str.upper().eq("Y")
    df["test_issue"] = df["Test Issue"].fillna("N").astype(str).str.upper().eq("Y")
    return df.rename(columns={"ACT Symbol": "Symbol"})[["Symbol", "name", "etf", "test_issue", "source"]]


BAD_PATTERNS = [
    " warrant", " warrants", " right", " rights", " unit", " units", " preferred", " depositary", " depository",
    " adr", " ads", " note", " notes", " bond", " etn", " nextshares", " when issued", " due ", " rate ",
    " income cap", " preferred stock", " preference", " senior note", " trust preferred"
]
GOOD_STOCK_PATTERNS = [
    " common stock", " common shares", " ordinary shares", " common share", " ordinary share", " class a common", " class b common"
]
DEFAULT_BAD_SYMBOLS_FILE = Path(__file__).resolve().parent / 'data' / 'universe' / 'yahoo_bad_symbols.txt'
BAD_SYMBOL_SUFFIXES = (
    '-V', '.V',
    '-WI', '.WI',
    '-WD', '.WD',
    '-WS', '.WS',
    '-W', '.W',
    '-U', '.U',
    '-R', '.R',
    '-RT', '.RT',
    '-P', '.P',
)
BAD_SYMBOL_SUBSTRINGS = ('^', '/', '=')


def load_known_bad_symbols(path: Path | None = None) -> set[str]:
    target = Path(path) if path else DEFAULT_BAD_SYMBOLS_FILE
    if not target.exists():
        return set()
    out = set()
    for raw in target.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        upper = line.upper()
        out.add(upper)
        out.add(upper.replace('.', '-'))
    return out


KNOWN_BAD_SYMBOLS = load_known_bad_symbols()


def is_probably_yahoo_friendly_symbol(symbol: str) -> bool:
    sym = str(symbol or '').upper().strip()
    if not sym:
        return False
    yahoo_sym = yahoo_symbol(sym)
    if sym in KNOWN_BAD_SYMBOLS or yahoo_sym in KNOWN_BAD_SYMBOLS:
        return False
    if any(ch in sym for ch in ["$", "+", "*"]):
        return False
    if any(token in sym for token in BAD_SYMBOL_SUBSTRINGS):
        return False
    if any(sym.endswith(suffix) for suffix in BAD_SYMBOL_SUFFIXES):
        return False
    if any(yahoo_sym.endswith(suffix.replace('.', '-')) for suffix in BAD_SYMBOL_SUFFIXES):
        return False
    return True


def is_regular_security(symbol: str, name: str, is_etf: bool, test_issue: bool) -> bool:
    if not symbol or test_issue:
        return False
    sym = symbol.upper()
    if not is_probably_yahoo_friendly_symbol(sym):
        return False
    lname = f" {str(name).lower()} "
    if is_etf:
        if any(x in lname for x in ["etn", "exchange traded note", "nextshares", "trust preferred"]):
            return False
        return True
    if any(p in lname for p in BAD_PATTERNS):
        return False
    if " fund" in lname or " trust" in lname or " acquisition" in lname or " acquisition corp" in lname:
        return False
    if any(p in lname for p in GOOD_STOCK_PATTERNS):
        return True
    if any(token in lname for token in [" class a", " class b", " class c", " ordinary", " common"]):
        return True
    return False


def yahoo_symbol(sym: str) -> str:
    return sym.replace('.', '-')


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def split_into_shards(seq, shard_count):
    if shard_count <= 1 or len(seq) <= 1:
        return [list(seq)] if seq else []
    shard_count = max(1, min(int(shard_count), len(seq)))
    base = len(seq) // shard_count
    extra = len(seq) % shard_count
    out = []
    start = 0
    for idx in range(shard_count):
        size = base + (1 if idx < extra else 0)
        end = start + size
        if start < len(seq):
            out.append(list(seq[start:end]))
        start = end
    return out


def append_log(stderr_path: str, message: str):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(stderr_path, 'a', encoding='utf-8') as f:
        f.write(f"[{ts}] {message}\n")


def download_bars(symbols, period, stderr_path, batch=200, phase='DOWNLOAD', interval='1d', prepost=False):
    frames = []
    misses = set()
    total_batches = max(1, math.ceil(len(symbols) / batch)) if symbols else 0
    for group_idx, group in enumerate(chunked(symbols, batch), start=1):
        batch_start = time.time()
        append_log(
            stderr_path,
            f"{phase}_BATCH_START period={period} interval={interval} batch={group_idx}/{total_batches} size={len(group)} accumulated_ok={len(frames)} accumulated_miss={len(misses)}"
        )
        tickers = ' '.join(group)
        time.sleep(0.35 + random.uniform(0.0, 0.55))
        data = None
        last_error = None
        for attempt in range(1, 4):
            try:
                data = run_with_hard_timeout(
                    45,
                    lambda: yf.download(
                        tickers=tickers,
                        period=period,
                        interval=interval,
                        auto_adjust=False,
                        group_by='ticker',
                        progress=False,
                        threads=False,
                        prepost=prepost,
                        timeout=30,
                    )
                )
                if data is not None and len(data) != 0:
                    break
                last_error = RuntimeError('empty download result')
            except Exception as e:
                last_error = e
            wait_s = 0.8 * attempt + random.uniform(0.6, 1.8)
            append_log(
                stderr_path,
                f"{phase}_RETRY period={period} interval={interval} batch={group_idx}/{total_batches} attempt={attempt} size={len(group)} wait={wait_s:.2f}s error={last_error}"
            )
            if attempt < 3:
                time.sleep(wait_s)
        if data is None or len(data) == 0:
            append_log(
                stderr_path,
                f"{phase}_ERROR period={period} interval={interval} batch={group_idx}/{total_batches} sample={group[:5]} error={last_error}"
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
            f"{phase}_BATCH_DONE period={period} interval={interval} batch={group_idx}/{total_batches} size={len(group)} ok={batch_ok} miss={batch_miss} cumulative_ok={len(frames)} cumulative_miss={len(misses)} elapsed={elapsed:.2f}s"
        )
        time.sleep(0.15)
    out = {}
    for sym, df in frames:
        cols = {str(c).lower(): c for c in df.columns}
        date_col = None
        for candidate in ('date', 'datetime', 'timestamp'):
            if candidate in cols:
                date_col = cols[candidate]
                break
        needed = [date_col, cols.get('open'), cols.get('high'), cols.get('low'), cols.get('close'), cols.get('volume')]
        if any(c is None for c in needed):
            misses.add(sym)
            continue
        sdf = df[[date_col, cols['open'], cols['high'], cols['low'], cols['close'], cols['volume']]].copy()
        sdf.columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
        sdf['Date'] = pd.to_datetime(sdf['Date'], utc=True, errors='coerce').dt.tz_localize(None)
        sdf = sdf.dropna(subset=['Date']).sort_values('Date')
        if len(sdf) == 0 or sdf[['Open','High','Low','Close']].dropna(how='all').empty:
            misses.add(sym)
            continue
        out[sym] = sdf.reset_index(drop=True)
        misses.discard(sym)
    return out, misses


def default_intraday_30m_signal(signal_type: str = '無訊號') -> dict:
    return {
        'has_30m_signal': False,
        'signal_type': signal_type,
        'signal_priority': 0,
        'signal_time': None,
        'signal_detail': '',
    }


def analyze_intraday_30m_buy_signal(df30: pd.DataFrame, pullback_date: str | None = None) -> dict:
    result = default_intraday_30m_signal()
    if df30 is None or len(df30) < 12:
        return result

    x = df30.copy().dropna(subset=['Open', 'High', 'Low', 'Close']).reset_index(drop=True)
    if len(x) < 12:
        return result
    if pullback_date:
        try:
            cutoff = pd.Timestamp(str(pullback_date))
            x = x[x['Date'] >= cutoff].reset_index(drop=True)
        except Exception:
            pass
    if len(x) < 12:
        return result

    best = None
    for i in range(6, len(x)):
        base = x.iloc[max(0, i - 6):i]
        if len(base) < 4:
            continue
        prev_low = float(base['Low'].min())
        prev_high = float(base['High'].max())
        low_i = float(x.iloc[i]['Low'])
        close_i = float(x.iloc[i]['Close'])

        # 30m 破底翻後回前高
        if low_i < prev_low * 0.998 and close_i >= prev_low:
            trigger_high = max(prev_high, float(x.iloc[i]['High']))
            for j in range(i, min(len(x), i + 7)):
                close_j = float(x.iloc[j]['Close'])
                if close_j >= trigger_high * 0.998:
                    cand = {
                        'has_30m_signal': True,
                        'signal_type': '30m破底翻回前高',
                        'signal_priority': 2,
                        'signal_time': pd.Timestamp(x.iloc[j]['Date']).strftime('%Y-%m-%d %H:%M'),
                        'signal_detail': f'30m低點跌破前低後，{pd.Timestamp(x.iloc[j]["Date"]).strftime("%m-%d %H:%M")} 收回前高 {trigger_high:.2f}',
                    }
                    best = cand if best is None or cand['signal_priority'] > best['signal_priority'] else best
                    break

        # 30m 震倉後回前高
        body_low = min(float(x.iloc[i]['Open']), close_i)
        wick_ratio = ((body_low - low_i) / body_low) if body_low > 0 else 0.0
        if low_i < prev_low * 0.997 and wick_ratio >= 0.003:
            for j in range(i, min(len(x), i + 9)):
                close_j = float(x.iloc[j]['Close'])
                if close_j >= prev_high * 0.998:
                    cand = {
                        'has_30m_signal': True,
                        'signal_type': '30m震倉後回前高',
                        'signal_priority': 1,
                        'signal_time': pd.Timestamp(x.iloc[j]['Date']).strftime('%Y-%m-%d %H:%M'),
                        'signal_detail': f'30m震倉下插後，{pd.Timestamp(x.iloc[j]["Date"]).strftime("%m-%d %H:%M")} 收回前高 {prev_high:.2f}',
                    }
                    if best is None or cand['signal_priority'] > best['signal_priority']:
                        best = cand
                    break

    return best or result


def analyze_intraday_30m_short_signal(df30: pd.DataFrame, pullback_date: str | None = None) -> dict:
    result = default_intraday_30m_signal()
    if df30 is None or len(df30) < 12:
        return result

    x = df30.copy().dropna(subset=['Open', 'High', 'Low', 'Close']).reset_index(drop=True)
    if len(x) < 12:
        return result
    if pullback_date:
        try:
            cutoff = pd.Timestamp(str(pullback_date))
            x = x[x['Date'] >= cutoff].reset_index(drop=True)
        except Exception:
            pass
    if len(x) < 12:
        return result

    best = None
    for i in range(6, len(x)):
        base = x.iloc[max(0, i - 6):i]
        if len(base) < 4:
            continue
        prev_low = float(base['Low'].min())
        prev_high = float(base['High'].max())
        high_i = float(x.iloc[i]['High'])
        close_i = float(x.iloc[i]['Close'])

        # 30m 假突破後回前低
        if high_i > prev_high * 1.002 and close_i <= prev_high:
            trigger_low = min(prev_low, float(x.iloc[i]['Low']))
            for j in range(i, min(len(x), i + 7)):
                close_j = float(x.iloc[j]['Close'])
                if close_j <= trigger_low * 1.002:
                    cand = {
                        'has_30m_signal': True,
                        'signal_type': '30m假突破回前低',
                        'signal_priority': 2,
                        'signal_time': pd.Timestamp(x.iloc[j]['Date']).strftime('%Y-%m-%d %H:%M'),
                        'signal_detail': f'30m高點假突破前高後，{pd.Timestamp(x.iloc[j]["Date"]).strftime("%m-%d %H:%M")} 跌回前低 {trigger_low:.2f}',
                    }
                    best = cand if best is None or cand['signal_priority'] > best['signal_priority'] else best
                    break

        # 30m 上插震倉後回前低
        body_high = max(float(x.iloc[i]['Open']), close_i)
        wick_ratio = ((high_i - body_high) / body_high) if body_high > 0 else 0.0
        if high_i > prev_high * 1.003 and wick_ratio >= 0.003:
            for j in range(i, min(len(x), i + 9)):
                close_j = float(x.iloc[j]['Close'])
                if close_j <= prev_low * 1.002:
                    cand = {
                        'has_30m_signal': True,
                        'signal_type': '30m上插震倉後回前低',
                        'signal_priority': 1,
                        'signal_time': pd.Timestamp(x.iloc[j]['Date']).strftime('%Y-%m-%d %H:%M'),
                        'signal_detail': f'30m上插震倉後，{pd.Timestamp(x.iloc[j]["Date"]).strftime("%m-%d %H:%M")} 跌回前低 {prev_low:.2f}',
                    }
                    if best is None or cand['signal_priority'] > best['signal_priority']:
                        best = cand
                    break

    return best or result


def enrich_rows_with_intraday_30m(rows: list[dict], stderr_path: str) -> list[dict]:
    if not rows:
        return rows
    symbol_map = {yahoo_symbol(str(row['symbol'])): row for row in rows if row.get('direction') in {'做多', '做空'}}
    if not symbol_map:
        return rows
    intraday, misses = download_bars(
        list(symbol_map.keys()),
        '1mo',
        stderr_path,
        batch=20,
        phase='INTRADAY30M',
        interval='30m',
        prepost=False,
    )
    append_log(stderr_path, f"INTRADAY30M_DONE ok={len(intraday)} miss={len(misses)}")
    kept_rows = []
    for ys, row in symbol_map.items():
        df30 = intraday.get(ys)
        if df30 is None:
            signal = default_intraday_30m_signal('無數據')
        elif row.get('direction') == '做空':
            signal = analyze_intraday_30m_short_signal(df30, pullback_date=row.get('pullback_date'))
        else:
            signal = analyze_intraday_30m_buy_signal(df30, pullback_date=row.get('pullback_date'))
        row['intraday_30m_signal'] = signal
        row['intraday_30m_status'] = '有' if signal.get('has_30m_signal') else '無'
        row['intraday_30m_signal_time'] = signal.get('signal_time')
        row['intraday_30m_priority'] = int(signal.get('signal_priority', 0) or 0)
        row['intraday_30m_detail'] = signal.get('signal_detail', '')
        
        # 修改2：30m 信號加分下調至 +8，且僅在回調日當日或次日上午有效
        if signal.get('has_30m_signal'):
            signal_time_str = signal.get('signal_time')
            pullback_date_str = row.get('pullback_date')
            add_bonus = False
            if signal_time_str and pullback_date_str:
                try:
                    signal_dt = pd.Timestamp(signal_time_str)
                    pullback_dt = pd.Timestamp(str(pullback_date_str))
                    # 只允許回調日當日或次日上午 (約 36 小時內)
                    hours_diff = (signal_dt - pullback_dt).total_seconds() / 3600
                    if 0 <= hours_diff <= INTRADAY_30M_MAX_HOURS_AFTER:
                        add_bonus = True
                except Exception:
                    pass
            if add_bonus:
                row['score'] = round(float(row.get('score', 0.0)) + INTRADAY_30M_BONUS, 1)
            else:
                row['intraday_30m_status'] = '過期'
        needs_intraday_reversal = bool(row.get('needs_intraday_reversal'))
        if needs_intraday_reversal and not signal.get('has_30m_signal'):
            continue
        kept_rows.append(row)
    rows[:] = kept_rows
    return rows


def local_extrema(df: pd.DataFrame, kind: str, lookback=90, window=SWING_WINDOW):
    sdf = df.tail(lookback).reset_index(drop=True)
    arr = sdf['Low'].to_numpy() if kind == 'low' else sdf['High'].to_numpy()
    idxs = []
    for i in range(window, len(sdf)-window):
        segment = arr[i-window:i+window+1]
        if kind == 'low':
            if arr[i] == np.nanmin(segment):
                if int(np.argmin(segment)) == window:
                    idxs.append(i)
        else:
            if arr[i] == np.nanmax(segment):
                if int(np.argmax(segment)) == window:
                    idxs.append(i)
    return sdf, idxs[-10:]


def avg_body(df):
    s = (df['Close'] - df['Open']).abs()
    return float(s.mean()) if len(s) else 0.0


def avg_tr(df):
    prev_close = df['Close'].shift(1)
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - prev_close).abs(),
        (df['Low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.mean()) if len(tr.dropna()) else 0.0


def score_confirm_day(df, idx, bullish=True):
    if idx <= 0 or idx >= len(df):
        return 0.0
    row = df.iloc[idx]
    trailing = df.iloc[max(0, idx-20):idx]
    body = abs(row['Close'] - row['Open'])
    avg20_body = avg_body(trailing)
    avg20_vol = float(trailing['Volume'].mean()) if len(trailing) else 0.0
    score = 0.0
    if bullish and row['Close'] > row['Open']:
        score += CONFIRM_BODY_DIR_BONUS
    if (not bullish) and row['Close'] < row['Open']:
        score += CONFIRM_BODY_DIR_BONUS
    if avg20_body > 0 and body > avg20_body:
        score += CONFIRM_BODY_SIZE_BONUS
    if avg20_vol > 0 and row['Volume'] > avg20_vol:
        score += CONFIRM_VOLUME_BONUS
    return score


def reference_close_n_trading_days_ago(df, idx, days=DIRECTION_FILTER_DAYS):
    ref_idx = idx - days
    if idx < 0 or idx >= len(df) or ref_idx < 0:
        return None
    return float(df.iloc[ref_idx]['Close'])


def passes_direction_filter_on_idx(df, idx, bullish=True, days=DIRECTION_FILTER_DAYS, min_pct=DIRECTION_FILTER_MIN_PCT):
    ref_close = reference_close_n_trading_days_ago(df, idx, days=days)
    if ref_close is None:
        return False
    current_close = float(df.iloc[idx]['Close'])
    pct = (current_close / ref_close - 1.0) * 100.0
    if bullish:
        return pct >= min_pct
    return pct <= -min_pct


def trailing_avg_dollar_volume(df, idx, days=5):
    if idx < 0 or idx >= len(df):
        return None
    left = max(0, idx - days + 1)
    seg = df.iloc[left:idx+1].dropna(subset=['Close', 'Volume'])
    if len(seg) == 0:
        return None
    dv = seg['Close'].astype(float) * seg['Volume'].astype(float)
    if len(dv) == 0:
        return None
    return float(dv.mean())


def liquidity_band_from_avg_dollar_volume(avg_dollar_volume):
    if avg_dollar_volume is None or not np.isfinite(avg_dollar_volume):
        return None
    if avg_dollar_volume >= 50_000_000:
        return '50m_plus'
    if avg_dollar_volume >= 20_000_000:
        return '20m_to_50m'
    return None


def filter_recent_windows_by_direction(df, windows, bullish=True, days=DIRECTION_FILTER_DAYS, min_pct=DIRECTION_FILTER_MIN_PCT):
    filtered = []
    if not windows:
        return filtered
    date_to_idx = {
        pd.Timestamp(row['Date']).strftime('%Y-%m-%d'): i
        for i, row in df.iterrows()
    }
    for w in windows:
        rep_date = w.get('representative_date')
        if not rep_date:
            continue
        idx = date_to_idx.get(str(rep_date))
        if idx is None:
            continue
        avg_dollar_volume = trailing_avg_dollar_volume(df, idx, days=20)
        liquidity_band = liquidity_band_from_avg_dollar_volume(avg_dollar_volume)
        if not liquidity_band:
            continue
        new_w = dict(w)
        avg_dollar_volume_val = float(avg_dollar_volume if avg_dollar_volume is not None else 0.0)
        new_w['avg_20d_dollar_volume'] = round(avg_dollar_volume_val, 2)
        new_w['liquidity_band'] = liquidity_band
        filtered.append(new_w)
    return filtered


def date_to_index(df, date_str):
    matches = df.index[df['Date'].dt.strftime('%Y-%m-%d') == str(date_str)].tolist()
    return matches[0] if matches else None


def find_platform_zone(series, around_idx, direction='long'):
    left = max(0, around_idx - 10)
    right = min(len(series), around_idx + 1)
    seg = series.iloc[left:right]
    if len(seg) == 0:
        return None
    return float(seg.quantile(0.35)), float(seg.quantile(0.65))


def find_chip_dense_zone(df, around_idx, lookback=CHIP_LOOKBACK, bins=24):
    left = max(0, around_idx - lookback + 1)
    seg = df.iloc[left:around_idx+1].dropna(subset=['High', 'Low', 'Close', 'Volume'])
    if len(seg) < 8:
        return None
    price_low = float(seg['Low'].min())
    price_high = float(seg['High'].max())
    if not np.isfinite(price_low) or not np.isfinite(price_high) or price_high <= price_low:
        return None
    edges = np.linspace(price_low, price_high, bins + 1)
    weights = np.zeros(bins, dtype=float)
    for _, row in seg.iterrows():
        lo = float(row['Low'])
        hi = float(row['High'])
        vol = max(float(row['Volume']), 0.0)
        if not np.isfinite(lo) or not np.isfinite(hi) or hi < lo:
            continue
        if hi == lo:
            idx = int(np.clip(np.searchsorted(edges, lo, side='right') - 1, 0, bins - 1))
            weights[idx] += vol
            continue
        touched = np.where((edges[:-1] < hi) & (edges[1:] > lo))[0]
        if len(touched) == 0:
            idx = int(np.clip(np.searchsorted(edges, (lo + hi) / 2.0, side='right') - 1, 0, bins - 1))
            weights[idx] += vol
            continue
        span = hi - lo
        for idx in touched:
            overlap = max(0.0, min(hi, edges[idx + 1]) - max(lo, edges[idx]))
            if overlap > 0:
                weights[idx] += vol * (overlap / span)
    if float(weights.sum()) <= 0:
        return None
    peak_idx = int(np.argmax(weights))
    peak_mid = float((edges[peak_idx] + edges[peak_idx + 1]) / 2.0)
    width = max((price_high - price_low) / bins * 1.5, peak_mid * 0.006)
    return peak_mid - width, peak_mid + width, peak_mid


def _recent_desc_break_from_anchors(df, confirm_idx, anchors, min_gap=3, overshoot_tol=0.0075):
    if len(anchors) < 2:
        return None
    for b in range(len(anchors) - 1, 0, -1):
        for a in range(b - 1, -1, -1):
            idx1, p1 = anchors[a]
            idx2, p2 = anchors[b]
            if idx2 - idx1 < min_gap or p2 >= p1:
                continue
            slope = (p2 - p1) / (idx2 - idx1)
            line_at_confirm = p1 + slope * (confirm_idx - idx1)
            close_confirm = float(df.iloc[confirm_idx]['Close'])
            if close_confirm <= line_at_confirm:
                continue
            seg_ok = True
            for t in range(idx1 + 1, confirm_idx):
                line_t = p1 + slope * (t - idx1)
                if float(df.iloc[t]['Close']) > line_t * (1.0 + overshoot_tol):
                    seg_ok = False
                    break
            if not seg_ok:
                continue
            prev_idx = max(idx2, confirm_idx - 3)
            prev_close = float(df.iloc[prev_idx]['Close']) if prev_idx < confirm_idx else float(df.iloc[confirm_idx - 1]['Close'])
            line_prev = p1 + slope * (prev_idx - idx1)
            recent_touch = prev_close <= line_prev * (1.0 + overshoot_tol)
            return {
                'anchor1': idx1,
                'anchor2': idx2,
                'line_value': line_at_confirm,
                'recent_touch': recent_touch,
            }
    return None


def _recent_asc_break_from_anchors(df, confirm_idx, anchors, min_gap=3, overshoot_tol=0.0075):
    if len(anchors) < 2:
        return None
    for b in range(len(anchors) - 1, 0, -1):
        for a in range(b - 1, -1, -1):
            idx1, p1 = anchors[a]
            idx2, p2 = anchors[b]
            if idx2 - idx1 < min_gap or p2 <= p1:
                continue
            slope = (p2 - p1) / (idx2 - idx1)
            line_at_confirm = p1 + slope * (confirm_idx - idx1)
            close_confirm = float(df.iloc[confirm_idx]['Close'])
            if close_confirm >= line_at_confirm:
                continue
            seg_ok = True
            for t in range(idx1 + 1, confirm_idx):
                line_t = p1 + slope * (t - idx1)
                if float(df.iloc[t]['Close']) < line_t * (1.0 - overshoot_tol):
                    seg_ok = False
                    break
            if not seg_ok:
                continue
            prev_idx = max(idx2, confirm_idx - 3)
            prev_close = float(df.iloc[prev_idx]['Close']) if prev_idx < confirm_idx else float(df.iloc[confirm_idx - 1]['Close'])
            line_prev = p1 + slope * (prev_idx - idx1)
            recent_touch = prev_close >= line_prev * (1.0 - overshoot_tol)
            return {
                'anchor1': idx1,
                'anchor2': idx2,
                'line_value': line_at_confirm,
                'recent_touch': recent_touch,
            }
    return None


def _fallback_desc_anchors(seg, start, max_points=12):
    highs = seg['High'].astype(float)
    ranked = sorted(range(len(seg) - 1), key=lambda i: (float(highs.iloc[i]), -i), reverse=True)
    out = []
    seen = set()
    for idx in ranked:
        if idx in seen:
            continue
        seen.add(idx)
        out.append((start + idx, float(highs.iloc[idx])))
        if len(out) >= max_points:
            break
    out.sort(key=lambda x: x[0])
    return out


def _fallback_asc_anchors(seg, start, max_points=12):
    lows = seg['Low'].astype(float)
    ranked = sorted(range(len(seg) - 1), key=lambda i: (float(lows.iloc[i]), i))
    out = []
    seen = set()
    for idx in ranked:
        if idx in seen:
            continue
        seen.add(idx)
        out.append((start + idx, float(lows.iloc[idx])))
        if len(out) >= max_points:
            break
    out.sort(key=lambda x: x[0])
    return out


def find_recent_desc_trendline_break(df, confirm_idx, lookback=SHORT_TREND_LOOKBACK, window=SWING_WINDOW):
    start = max(0, confirm_idx - lookback)
    seg = df.iloc[start:confirm_idx+1].reset_index(drop=True)
    _, highs = local_extrema(seg, 'high', lookback=len(seg), window=window)
    anchors = [(start + idx, float(seg.iloc[idx]['High'])) for idx in highs]
    result = _recent_desc_break_from_anchors(df, confirm_idx, anchors)
    if result is not None:
        result['source'] = 'swing'
        return result
    fallback_anchors = _fallback_desc_anchors(seg, start)
    result = _recent_desc_break_from_anchors(df, confirm_idx, fallback_anchors)
    if result is not None:
        result['source'] = 'fallback'
        return result
    return None


def find_recent_asc_trendline_break(df, confirm_idx, lookback=SHORT_TREND_LOOKBACK, window=SWING_WINDOW):
    start = max(0, confirm_idx - lookback)
    seg = df.iloc[start:confirm_idx+1].reset_index(drop=True)
    _, lows = local_extrema(seg, 'low', lookback=len(seg), window=window)
    anchors = [(start + idx, float(seg.iloc[idx]['Low'])) for idx in lows]
    result = _recent_asc_break_from_anchors(df, confirm_idx, anchors)
    if result is not None:
        result['source'] = 'swing'
        return result
    fallback_anchors = _fallback_asc_anchors(seg, start)
    result = _recent_asc_break_from_anchors(df, confirm_idx, fallback_anchors)
    if result is not None:
        result['source'] = 'fallback'
        return result
    return None


def qualifies_reclaim_after_fib_break_long(df, fib618, idx, max_days=5):
    close = float(df.iloc[idx]['Close'])
    low = float(df.iloc[idx]['Low'])
    if not (close < fib618 and low < fib618):
        return True, False
    end = min(len(df) - 1, idx + max_days)
    for j in range(idx + 1, end + 1):
        row = df.iloc[j]
        prev = df.iloc[j - 1]
        bullish_candle = float(row['Close']) > float(row['Open']) and float(row['Close']) >= fib618
        gap_reclaim = float(row['Open']) >= fib618 and float(prev['Close']) < fib618
        if bullish_candle or gap_reclaim:
            return True, True
    return False, False


def qualifies_reclaim_after_fib_break_short(df, fib618, idx, max_days=5):
    close = float(df.iloc[idx]['Close'])
    high = float(df.iloc[idx]['High'])
    if not (close > fib618 and high > fib618):
        return True, False
    end = min(len(df) - 1, idx + max_days)
    for j in range(idx + 1, end + 1):
        row = df.iloc[j]
        prev = df.iloc[j - 1]
        bearish_candle = float(row['Close']) < float(row['Open']) and float(row['Close']) <= fib618
        gap_reclaim = float(row['Open']) <= fib618 and float(prev['Close']) > fib618
        if bearish_candle or gap_reclaim:
            return True, True
    return False, False


def nearest_swing_high(df, start_idx, end_idx):
    if end_idx <= start_idx:
        return None
    seg = df.iloc[start_idx:end_idx+1]
    if len(seg) == 0:
        return None
    idx = int(seg['High'].idxmax())
    return idx


def nearest_swing_low(df, start_idx, end_idx):
    if end_idx <= start_idx:
        return None
    seg = df.iloc[start_idx:end_idx+1]
    if len(seg) == 0:
        return None
    idx = int(seg['Low'].idxmin())
    return idx


def pct_diff(a, b):
    denom = (abs(a)+abs(b))/2.0
    return abs(a-b)/denom if denom else 999


def has_higher_high_between(df, left_idx, right_idx, ceiling):
    if right_idx - left_idx <= 1:
        return False
    seg = df.iloc[left_idx + 1:right_idx]
    if len(seg) == 0:
        return False
    return float(seg['High'].max()) > float(ceiling)


def has_lower_low_between(df, left_idx, right_idx, floor):
    if right_idx - left_idx <= 1:
        return False
    seg = df.iloc[left_idx + 1:right_idx]
    if len(seg) == 0:
        return False
    return float(seg['Low'].min()) < float(floor)


def breaks_below_level_after(df, start_idx, level):
    if start_idx >= len(df) - 1:
        return False
    seg = df.iloc[start_idx + 1:]
    if len(seg) == 0:
        return False
    return float(seg['Low'].min()) < float(level)


def breaks_above_level_after(df, start_idx, level):
    if start_idx >= len(df) - 1:
        return False
    seg = df.iloc[start_idx + 1:]
    if len(seg) == 0:
        return False
    return float(seg['High'].max()) > float(level)


def make_result(symbol, direction, pattern, zone, event_date, confirm_date, pullback_date, price, fib618, volume_feature, slowdown_feature, score, logic, recent_windows=None):
    return {
        'symbol': symbol,
        'direction': direction,
        'pattern': pattern,
        'zone': zone,
        'event_date': event_date.strftime('%Y-%m-%d'),
        'confirm_date': confirm_date.strftime('%Y-%m-%d'),
        'pullback_date': pullback_date.strftime('%Y-%m-%d'),
        'price': round(float(price), 2),
        'fib618': round(float(fib618), 2),
        'volume_feature': volume_feature,
        'slowdown_feature': slowdown_feature,
        'score': round(float(score), 1),
        'logic': logic,
        'recent_windows': recent_windows or [],
        '_sort_pullback': pullback_date,
        '_sort_event': event_date,
        '_sort_confirm': confirm_date,
    }


def build_long_pullback_after_double_top(symbol, df, top1_idx, top2_idx, valley_idx):
    top1_high = float(df.iloc[top1_idx]['High'])
    top2_high = float(df.iloc[top2_idx]['High'])
    valley_low = float(df.iloc[valley_idx]['Low'])
    if valley_low >= top2_high:
        return None
    if has_higher_high_between(df, top1_idx, top2_idx, max(top1_high, top2_high)):
        return None
    if breaks_below_level_after(df, top2_idx, valley_low):
        return None

    fib50 = top2_high - 0.5 * (top2_high - valley_low)
    fib618 = top2_high - 0.618 * (top2_high - valley_low)
    if fib50 < fib618:
        fib50, fib618 = fib618, fib50

    recent_valley_search_end = min(len(df) - 1, top2_idx + 31)
    if recent_valley_search_end - (top2_idx + 3) < 3:
        return None

    recent_valley_idx = min(range(top2_idx + 3, recent_valley_search_end), key=lambda idx: float(df.iloc[idx]['Low']))
    recent_valley_low = float(df.iloc[recent_valley_idx]['Low'])
    drop_pct = (top2_high / recent_valley_low - 1.0) * 100.0 if recent_valley_low > 0 else 0.0
    if drop_pct < 12.0:
        return None

    chip_zone = find_chip_dense_zone(df, recent_valley_idx, lookback=30)
    platform_zone = find_platform_zone(df['Close'], recent_valley_idx, direction='long')
    best = None
    pullback_candidates = []
    double_top_mid = (top1_high + top2_high) / 2.0

    for k in range(top2_idx + 5, min(len(df) - 1, top2_idx + 31)):
        low = float(df.iloc[k]['Low'])
        high = float(df.iloc[k]['High'])
        close = float(df.iloc[k]['Close'])
        if not (low <= float(df.iloc[k - 1]['Low']) and low <= float(df.iloc[k + 1]['Low'])):
            continue

        in_fib_zone = fib618 * 0.995 <= low <= fib50 * 1.005
        if not in_fib_zone:
            continue
        if low <= valley_low * 1.002:
            continue

        touch_chip = False
        chip_low = chip_high = chip_mid = None
        if chip_zone:
            chip_low, chip_high, chip_mid = chip_zone
            touch_chip = (chip_low * 0.99 <= low <= chip_high * 1.01) or (chip_low * 0.99 <= close <= chip_high * 1.01)

        touch_platform = False
        platform_low = platform_high = None
        if platform_zone:
            platform_low, platform_high = platform_zone
            touch_platform = (platform_low * 0.992 <= low <= platform_high * 1.008) or (platform_low * 0.992 <= close <= platform_high * 1.008)

        touch_double_top_mid = abs(low / double_top_mid - 1.0) <= 0.03
        touch_recent_drop_low = abs(low / recent_valley_low - 1.0) <= 0.005
        support_count = sum(1 for flag in [touch_chip, touch_platform, touch_double_top_mid, touch_recent_drop_low] if flag)
        if support_count == 0:
            continue

        rebound_follow_idx = None
        trendline_break = None
        for j in range(k + 1, min(len(df), k + 6)):
            if float(df.iloc[j]['Close']) > float(df.iloc[j - 1]['High']):
                trendline_break = find_recent_desc_trendline_break(df, j, lookback=SHORT_TREND_LOOKBACK, window=SWING_WINDOW)
                if trendline_break is None:
                    continue
                rebound_follow_idx = j
                break
        daily_rebound_confirmed = rebound_follow_idx is not None
        needs_intraday_reversal = rebound_follow_idx is None

        vol20 = trailing_avg_dollar_volume(df, k, days=20) or 0.0
        pullback_day_dv = float(df.iloc[k]['Close']) * float(df.iloc[k]['Volume'])
        vol_shrink = vol20 > 0 and pullback_day_dv < vol20
        above_20d_close = False
        close_20d_ago = None
        if k >= 20:
            close_20d_ago = float(df.iloc[k - 20]['Close'])
            above_20d_close = close > close_20d_ago * 1.01

        decline_seg = df.iloc[top2_idx:k + 1]
        slowdown = 0
        if len(decline_seg) >= 4:
            tail_seg = decline_seg.iloc[-3:]
            head_seg = decline_seg.iloc[:-3]
            if len(head_seg) >= 2:
                if avg_body(tail_seg) < avg_body(head_seg) * 0.85:
                    slowdown += 1
                if avg_tr(tail_seg) < avg_tr(head_seg) * 0.90:
                    slowdown += 1

        entry_anchor_idx = rebound_follow_idx if rebound_follow_idx is not None else k
        entry_price = max(float(df.iloc[entry_anchor_idx]['Close']), high)
        if touch_chip and chip_low is not None:
            stop_price = chip_low * 0.985
        elif platform_low is not None:
            stop_price = platform_low * 0.985
        else:
            stop_price = low * 0.985
        risk = entry_price - stop_price
        if risk <= 0:
            continue

        target1 = double_top_mid
        if target1 <= entry_price:
            target1 = max(top2_high, entry_price + max(risk * 1.2, entry_price * 0.03))
        target2 = low + 1.618 * (top2_high - recent_valley_low)
        if target2 <= target1:
            target2 = max(target1 + risk, entry_price + 1.618 * risk)

        bonus = 0.0
        bonus += 10 if touch_chip else 0       # 籌碼密集區 +10
        bonus += 6 if touch_platform else 0    # 平台區 +6
        bonus += 4 if touch_double_top_mid else 0  # 雙頂中軸 +4
        bonus += 8 if in_fib_zone else 0       # Fib 區間 +8
        bonus += 6 if vol_shrink else 0
        bonus += slowdown * 4
        bonus += 5 if above_20d_close else 0
        bonus += 4 if daily_rebound_confirmed and rebound_follow_idx - k <= 2 else 0
        # 長期跨度加分：每 10 日 +1 分，上限 15 分
        long_span_bonus = min((top2_idx - top1_idx) // 10 * LONG_SPAN_BONUS_PER_10D, LONG_SPAN_BONUS_MAX)
        bonus += long_span_bonus
        if needs_intraday_reversal:
            bonus -= 3

        zone_parts = [f"回踩 大升段0.5-0.618 ({fib50:.2f}/{fib618:.2f})"]
        if touch_chip and chip_mid is not None:
            zone_parts.append(f"籌碼密集區 {chip_low:.2f}-{chip_high:.2f} / 中軸 {chip_mid:.2f}")
        if touch_platform and platform_low is not None and platform_high is not None:
            zone_parts.append(f"平台區 {platform_low:.2f}-{platform_high:.2f}")
        if touch_double_top_mid:
            zone_parts.append(f"雙頂中軸 {double_top_mid:.2f}")
        if above_20d_close and close_20d_ago is not None:
            zone_parts.append(f"回調價高於20日前收市 {close_20d_ago:.2f}")

        score = 52.0 + bonus + max(0.0, 10.0 - max(0, (top2_idx - top1_idx) - 20) * 0.15)
        score += max(0.0, 10.0 - pct_diff(top1_high, top2_high) * 400.0)
        candidate = make_result(
            symbol=symbol,
            direction='做多',
            pattern='雙頂→右側回調買點',
            zone=' / '.join(zone_parts),
            event_date=df.iloc[valley_idx]['Date'],
            confirm_date=df.iloc[entry_anchor_idx]['Date'],
            pullback_date=df.iloc[k]['Date'],
            price=df.iloc[-1]['Close'],
            fib618=fib618,
            volume_feature='回調量縮' if vol_shrink else '一般',
            slowdown_feature='回調減速' if slowdown >= 1 else '回調正常',
            score=score,
            logic='先找雙頂，再以雙頂之間的主升段低點到第二頂高點量度回調；若第二頂後先出現一段明顯下跌，之後回踩0.5-0.618並靠近籌碼/平台，且1-5日內重新轉強，視作右側回調買點。',
            recent_windows=[],
        )
        candidate.update({
            'entry_price': round(float(entry_price), 2),
            'double_top_1_date': df.iloc[top1_idx]['Date'].strftime('%Y-%m-%d'),
            'double_top_2_date': df.iloc[top2_idx]['Date'].strftime('%Y-%m-%d'),
            'double_top_mid': round(double_top_mid, 2),
            'double_top_gap_days': int(top2_idx - top1_idx),
            'valley_date': df.iloc[valley_idx]['Date'].strftime('%Y-%m-%d'),
            'valley_low': round(valley_low, 2),
            'recent_drop_date': df.iloc[recent_valley_idx]['Date'].strftime('%Y-%m-%d'),
            'recent_drop_low': round(recent_valley_low, 2),
            'trend_break_date': df.iloc[entry_anchor_idx]['Date'].strftime('%Y-%m-%d'),
            'chip_zone_low': round(float(chip_low), 2) if chip_low is not None else None,
            'chip_zone_high': round(float(chip_high), 2) if chip_high is not None else None,
            'chip_zone_mid': round(float(chip_mid), 2) if chip_mid is not None else None,
            'support_flags': {
                'touch_chip': touch_chip,
                'touch_platform': touch_platform,
                'touch_double_top_mid': touch_double_top_mid,
                'touch_recent_drop_low': touch_recent_drop_low,
                'above_20d_close': above_20d_close,
                'close_20d_ago': round(float(close_20d_ago), 2) if close_20d_ago is not None else None,
                'support_count': support_count,
                'fallback_mode': 'double_top_right_side_pullback',
            },
            'risk_reward_1': round(float((target1 - entry_price) / risk), 2) if target1 > entry_price else None,
            'daily_rebound_confirmed': daily_rebound_confirmed,
            'needs_intraday_reversal': needs_intraday_reversal,
        })
        pullback_candidates.append({'idx': k, 'price_level': low})
        if best is None or candidate['score'] > best['score'] or (math.isclose(candidate['score'], best['score']) and candidate['_sort_pullback'] > best['_sort_pullback']):
            best = candidate

    if best is not None:
        best['recent_windows'] = filter_recent_windows_by_direction(
            df,
            build_recent_windows(df, pullback_candidates, bullish=True, max_windows=3, max_gap_days=3),
            bullish=True,
            days=20,
            min_pct=1.0,
        )
    return best


def build_short_right_shoulder_after_double_bottom(symbol, df, low1_idx, low2_idx, peak_idx):
    low1_price = float(df.iloc[low1_idx]['Low'])
    low2_price = float(df.iloc[low2_idx]['Low'])
    peak_high = float(df.iloc[peak_idx]['High'])
    if peak_high <= low2_price:
        return None
    if has_lower_low_between(df, low1_idx, low2_idx, min(low1_price, low2_price)):
        return None
    if breaks_above_level_after(df, low2_idx, peak_high):
        return None

    fib50 = low2_price + 0.5 * (peak_high - low2_price)
    fib618 = low2_price + 0.618 * (peak_high - low2_price)
    if fib50 > fib618:
        fib50, fib618 = fib618, fib50

    recent_peak_search_end = min(len(df) - 1, low2_idx + 31)
    if recent_peak_search_end - (low2_idx + 3) < 3:
        return None

    recent_peak_idx = max(range(low2_idx + 3, recent_peak_search_end), key=lambda idx: float(df.iloc[idx]['High']))
    recent_peak_high = float(df.iloc[recent_peak_idx]['High'])
    rise_pct = (recent_peak_high / low2_price - 1.0) * 100.0 if low2_price > 0 else 0.0
    if rise_pct < 12.0:
        return None

    chip_zone = find_chip_dense_zone(df, recent_peak_idx, lookback=30)
    platform_zone = find_platform_zone(df['Close'], recent_peak_idx, direction='short')
    best = None
    pullback_candidates = []
    double_bottom_mid = (low1_price + low2_price) / 2.0

    for k in range(low2_idx + 5, min(len(df) - 1, low2_idx + 31)):
        high = float(df.iloc[k]['High'])
        low = float(df.iloc[k]['Low'])
        close = float(df.iloc[k]['Close'])
        if not (high >= float(df.iloc[k - 1]['High']) and high >= float(df.iloc[k + 1]['High'])):
            continue

        in_fib_zone = fib50 * 0.995 <= high <= fib618 * 1.005
        if not in_fib_zone:
            continue
        if high >= peak_high * 0.998:
            continue

        touch_chip = False
        chip_low = chip_high = chip_mid = None
        if chip_zone:
            chip_low, chip_high, chip_mid = chip_zone
            touch_chip = (chip_low * 0.99 <= high <= chip_high * 1.01) or (chip_low * 0.99 <= close <= chip_high * 1.01)

        touch_platform = False
        platform_low = platform_high = None
        if platform_zone:
            platform_low, platform_high = platform_zone
            touch_platform = (platform_low * 0.992 <= high <= platform_high * 1.008) or (platform_low * 0.992 <= close <= platform_high * 1.008)

        touch_double_bottom_mid = abs(high / double_bottom_mid - 1.0) <= 0.03
        touch_recent_rally_high = abs(high / recent_peak_high - 1.0) <= 0.005
        support_count = sum(1 for flag in [touch_chip, touch_platform, touch_double_bottom_mid, touch_recent_rally_high] if flag)
        if support_count == 0:
            continue

        breakdown_follow_idx = None
        trendline_break = None
        for j in range(k + 1, min(len(df), k + 6)):
            if float(df.iloc[j]['Close']) < float(df.iloc[j - 1]['Low']):
                trendline_break = find_recent_asc_trendline_break(df, j, lookback=SHORT_TREND_LOOKBACK, window=SWING_WINDOW)
                if trendline_break is None:
                    continue
                if float(df.iloc[j]['Close']) > trendline_break['line_value'] * 1.002:
                    continue
                breakdown_follow_idx = j
                break
        daily_breakdown_confirmed = breakdown_follow_idx is not None
        needs_intraday_reversal = breakdown_follow_idx is None

        vol20 = trailing_avg_dollar_volume(df, k, days=20) or 0.0
        pullback_day_dv = float(df.iloc[k]['Close']) * float(df.iloc[k]['Volume'])
        vol_shrink = vol20 > 0 and pullback_day_dv < vol20
        below_20d_close = False
        close_20d_ago = None
        if k >= 20:
            close_20d_ago = float(df.iloc[k - 20]['Close'])
            below_20d_close = close < close_20d_ago * 0.99

        rise_seg = df.iloc[low2_idx:k + 1]
        slowdown = 0
        if len(rise_seg) >= 4:
            tail_seg = rise_seg.iloc[-3:]
            head_seg = rise_seg.iloc[:-3]
            if len(head_seg) >= 2:
                if avg_body(tail_seg) < avg_body(head_seg) * 0.85:
                    slowdown += 1
                if avg_tr(tail_seg) < avg_tr(head_seg) * 0.90:
                    slowdown += 1

        entry_anchor_idx = breakdown_follow_idx if breakdown_follow_idx is not None else k
        entry_price = min(float(df.iloc[entry_anchor_idx]['Close']), low)
        if touch_chip and chip_high is not None:
            stop_price = chip_high * 1.015
        elif platform_high is not None:
            stop_price = platform_high * 1.015
        else:
            stop_price = high * 1.015
        risk = stop_price - entry_price
        if risk <= 0:
            continue

        target1 = double_bottom_mid
        if target1 >= entry_price:
            target1 = min(low2_price, entry_price - max(risk * 1.2, entry_price * 0.03))
        target2 = high - 1.618 * (high - low2_price)
        if target2 >= target1:
            target2 = min(target1 - risk, entry_price - 1.618 * risk)

        bonus = 0.0
        bonus += 10 if touch_chip else 0       # 籌碼密集區 +10
        bonus += 6 if touch_platform else 0    # 平台區 +6
        bonus += 4 if touch_double_bottom_mid else 0  # 雙底中軸 +4
        bonus += 8 if in_fib_zone else 0       # Fib 區間 +8
        bonus += 6 if vol_shrink else 0
        bonus += slowdown * 4
        bonus += 5 if below_20d_close else 0
        bonus += 4 if daily_breakdown_confirmed and breakdown_follow_idx - k <= 2 else 0
        # 長期跨度加分：每 10 日 +1 分，上限 15 分
        long_span_bonus = min((low2_idx - low1_idx) // 10 * LONG_SPAN_BONUS_PER_10D, LONG_SPAN_BONUS_MAX)
        bonus += long_span_bonus
        if needs_intraday_reversal:
            bonus -= 3

        zone_parts = [f"回抽 大跌段0.5-0.618 ({fib50:.2f}/{fib618:.2f})"]
        if touch_chip and chip_mid is not None:
            zone_parts.append(f"籌碼密集區 {chip_low:.2f}-{chip_high:.2f} / 中軸 {chip_mid:.2f}")
        if touch_platform and platform_low is not None and platform_high is not None:
            zone_parts.append(f"平台區 {platform_low:.2f}-{platform_high:.2f}")
        if touch_double_bottom_mid:
            zone_parts.append(f"雙底中軸 {double_bottom_mid:.2f}")
        if below_20d_close and close_20d_ago is not None:
            zone_parts.append(f"回抽價低於20日前收市 {close_20d_ago:.2f}")

        score = 52.0 + bonus + max(0.0, 10.0 - max(0, (low2_idx - low1_idx) - 20) * 0.15)
        score += max(0.0, 10.0 - pct_diff(low1_price, low2_price) * 400.0)
        candidate = make_result(
            symbol=symbol,
            direction='做空',
            pattern='雙底→右肩回調賣點',
            zone=' / '.join(zone_parts),
            event_date=df.iloc[peak_idx]['Date'],
            confirm_date=df.iloc[entry_anchor_idx]['Date'],
            pullback_date=df.iloc[k]['Date'],
            price=df.iloc[-1]['Close'],
            fib618=fib618,
            volume_feature='回抽量縮' if vol_shrink else '一般',
            slowdown_feature='回抽減速' if slowdown >= 1 else '回抽正常',
            score=score,
            logic='先找雙底，再以雙底之間的主要跌段高點到第二底低點量度回抽；若後續反彈只到0.5-0.618、靠近籌碼/平台且1-5日內重新跌破短升勢，視作右肩回調賣點。',
            recent_windows=[],
        )
        candidate.update({
            'entry_price': round(float(entry_price), 2),
            'double_bottom_1_date': df.iloc[low1_idx]['Date'].strftime('%Y-%m-%d'),
            'double_bottom_2_date': df.iloc[low2_idx]['Date'].strftime('%Y-%m-%d'),
            'double_bottom_mid': round(double_bottom_mid, 2),
            'double_bottom_gap_days': int(low2_idx - low1_idx),
            'peak_date': df.iloc[peak_idx]['Date'].strftime('%Y-%m-%d'),
            'peak_high': round(peak_high, 2),
            'peak_low': round(float(df.iloc[peak_idx]['Low']), 2),
            'breakout_date': df.iloc[recent_peak_idx]['Date'].strftime('%Y-%m-%d'),
            'fake_breakdown_date': df.iloc[entry_anchor_idx]['Date'].strftime('%Y-%m-%d'),
            'breakout_ref_low': round(low, 2),
            'trend_break_date': df.iloc[entry_anchor_idx]['Date'].strftime('%Y-%m-%d'),
            'chip_zone_low': round(float(chip_low), 2) if chip_low is not None else None,
            'chip_zone_high': round(float(chip_high), 2) if chip_high is not None else None,
            'chip_zone_mid': round(float(chip_mid), 2) if chip_mid is not None else None,
            'support_flags': {
                'touch_chip': touch_chip,
                'touch_platform': touch_platform,
                'touch_double_bottom_mid': touch_double_bottom_mid,
                'touch_recent_rally_high': touch_recent_rally_high,
                'below_20d_close': below_20d_close,
                'close_20d_ago': round(float(close_20d_ago), 2) if close_20d_ago is not None else None,
                'support_count': support_count,
                'fallback_mode': 'double_bottom_right_shoulder',
            },
            'risk_reward_1': round(float((entry_price - target1) / risk), 2) if target1 < entry_price else None,
            'daily_breakdown_confirmed': daily_breakdown_confirmed,
            'needs_intraday_reversal': needs_intraday_reversal,
        })
        pullback_candidates.append({'idx': k, 'price_level': high})
        if best is None or candidate['score'] > best['score'] or (math.isclose(candidate['score'], best['score']) and candidate['_sort_pullback'] > best['_sort_pullback']):
            best = candidate

    if best is not None:
        best['recent_windows'] = filter_recent_windows_by_direction(
            df,
            build_recent_windows(df, pullback_candidates, bullish=False, max_windows=3, max_gap_days=3),
            bullish=False,
            days=20,
            min_pct=1.0,
        )
    return best


def build_recent_windows(df, points, bullish=True, max_windows=3, max_gap_days=3):
    if not points:
        return []
    ordered = sorted(points, key=lambda x: x['idx'])
    grouped = []
    current = [ordered[0]]
    for point in ordered[1:]:
        prev_date = df.iloc[current[-1]['idx']]['Date']
        point_date = df.iloc[point['idx']]['Date']
        gap_days = int((point_date - prev_date).days)
        if gap_days <= max_gap_days:
            current.append(point)
        else:
            grouped.append(current)
            current = [point]
    grouped.append(current)

    out = []
    for group in grouped[-max_windows:]:
        if bullish:
            rep = min(group, key=lambda x: (x['price_level'], x['idx']))
        else:
            rep = max(group, key=lambda x: (x['price_level'], -x['idx']))
        out.append({
            'start_date': df.iloc[group[0]['idx']]['Date'].strftime('%Y-%m-%d'),
            'end_date': df.iloc[group[-1]['idx']]['Date'].strftime('%Y-%m-%d'),
            'representative_date': df.iloc[rep['idx']]['Date'].strftime('%Y-%m-%d'),
            'representative_price': round(float(rep['price_level']), 2),
            'count': len(group),
        })
    return out


def clone_row_for_liquidity_band(row, band_key):
    band_windows = [w for w in (row.get('recent_windows') or []) if w.get('liquidity_band') == band_key]
    if not band_windows:
        return None
    new_row = dict(row)
    new_row['recent_windows'] = band_windows
    new_row['liquidity_band'] = band_key
    new_row['pullback_date'] = band_windows[-1]['representative_date']
    new_row['_sort_pullback'] = pd.Timestamp(band_windows[-1]['representative_date'])
    return new_row


def render_markdown_report(out: dict) -> str:
    lines = []
    lines.append("# 美股雙頂→破底翻→回調買點簡報")
    lines.append("")
    miss_total = int(out.get('stage1_misses', 0)) + int(out.get('stage2_misses', 0))
    miss_note = f"；数据下载失败 {miss_total} 个" if miss_total else ""
    lines.append(
        f"摘要：共扫描 {out.get('universe_total', 0)} 个标的，"
        f"通过流动性过滤 {out.get('liquid_count', 0)} 个，"
        f"深度扫描 {out.get('deep_scan_count', 0)} 个，"
        f"形成候选 {out.get('candidate_total', 0)} 个，"
        f"其中做多 {out.get('long_candidates', 0)} 个、做空 {out.get('short_candidates', 0)} 个，"
        f"最终输出前 {len(out.get('top10', []))} 个{miss_note}。"
    )
    lines.append("")
    top10 = out.get('top10', []) or []
    if not top10:
        lines.append("今日無符合『雙頂後破底翻成立，再等 0.5-0.618 回調共振買點』條件的標的。")
        if out.get('stderr_log'):
            lines.append("")
            lines.append(f"日志：`{out['stderr_log']}`")
        return "\n".join(lines)

    lines.append("| 代码 | 方向 | 形态 | 支撑/阻力区 | 破底翻日 | 确认日 | 最近回调日 | 现价 | 0.618关键位 | 量能特征 | 减速特征 | 质量分 | 一句话逻辑 |")
    lines.append("|---|---|---|---|---|---|---|---:|---:|---|---|---:|---|")

    def display_pullback_dates(row: dict) -> str:
        windows = row.get('recent_windows') or []
        if windows:
            return ' / '.join(w.get('representative_date', '') for w in windows if w.get('representative_date'))
        return row['pullback_date']

    for row in top10:
        lines.append(
            f"| {row['symbol']} | {row['direction']} | {row['pattern']} | {row['zone']} | {row['event_date']} | {row['confirm_date']} | {display_pullback_dates(row)} | {row['price']:.2f} | {row['fib618']:.2f} | {row['volume_feature']} | {row['slowdown_feature']} | {row['score']:.1f} | {row['logic']} |"
        )

    lines.append("")
    lines.append("## 观察要点")
    lines.append("")
    long_top = [x for x in top10 if x['direction'] == '做多']
    short_top = [x for x in top10 if x['direction'] == '做空']
    qty_shrink = sum(1 for x in top10 if x['volume_feature'] == '量缩')
    qty_slow = sum(1 for x in top10 if '减速' in x['slowdown_feature'])
    newest = top10[0]
    lines.append(f"- 今日最优先关注的是最近回调/回抽日最新的标的：**{newest['symbol']}**（{newest['direction']} / {newest['pattern']}）。")
    lines.append(f"- 前10中量缩回踩/回抽共有 **{qty_shrink}** 个，说明不少候选属于缩量测试关键区的类型。")
    lines.append(f"- 前10中出现减速回调/减速回抽特征的共有 **{qty_slow}** 个，这类通常更接近理想二次介入结构。")
    lines.append(f"- 多头候选 **{len(long_top)}** 个，空头候选 **{len(short_top)}** 个，可用来判断当天偏风险偏好还是偏防守。")
    lines.append("- 支撑/阻力区、筹码密集区中轴、0.5/0.618 位置均为日线近似计算，适合做盘后筛选，不替代盘中确认。")
    lines.append("- 若次日出现放量重新站上支撑/跌回阻力下方，通常比单纯到位但未确认的胜率更高。")
    return "\n".join(lines)


def scan_stage2_dataset(stage2, mapped, stderr_path):
    results = []
    long_count = 0
    short_count = 0
    for ys, df in stage2.items():
        try:
            df = df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume']).reset_index(drop=True)
            if len(df) < 120:
                continue
            long_r = scan_long(mapped[ys], df)
            short_r = scan_short(mapped[ys], df)
            if long_r:
                results.append(long_r)
                long_count += 1
            if short_r:
                results.append(short_r)
                short_count += 1
        except Exception as e:
            append_log(stderr_path, f"SCAN_ERROR {ys} {e}\\n{traceback.format_exc()}")
    return results, long_count, short_count


def scan_long(symbol, df):
    if len(df) < 90:
        return None
    df = df.copy().dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume']).reset_index(drop=True)
    if len(df) < 90:
        return None

    pivot_window = SWING_WINDOW
    pivot_lows = []
    pivot_highs = []
    for i in range(pivot_window, len(df) - pivot_window):
        low_seg = df['Low'].iloc[i-pivot_window:i+pivot_window+1]
        high_seg = df['High'].iloc[i-pivot_window:i+pivot_window+1]
        if float(df.iloc[i]['Low']) == float(low_seg.min()) and int(low_seg.argmin()) == pivot_window:
            pivot_lows.append(i)
        if float(df.iloc[i]['High']) == float(high_seg.max()) and int(high_seg.argmax()) == pivot_window:
            pivot_highs.append(i)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None

    candidates = []
    qualifying_pullbacks_all = []

    for top2_idx in pivot_highs:
        if top2_idx < 30 or top2_idx > len(df) - 10:
            continue
        top2_high = float(df.iloc[top2_idx]['High'])

        top1_candidates = [
            idx for idx in pivot_highs
            if top2_idx - 80 <= idx <= top2_idx - 20
            and pct_diff(float(df.iloc[idx]['High']), top2_high) <= 0.02
        ]
        if not top1_candidates:
            continue
        top1_idx = min(top1_candidates, key=lambda idx: abs((top2_idx - idx) - 15))
        top1_high = float(df.iloc[top1_idx]['High'])
        if has_higher_high_between(df, top1_idx, top2_idx, max(top1_high, top2_high)):
            continue
        double_top_mid = (top1_high + top2_high) / 2.0
        double_top_gap = top2_idx - top1_idx

        valley_candidates = [idx for idx in pivot_lows if top1_idx + 2 <= idx <= top2_idx - 2]
        if not valley_candidates:
            continue
        valley_idx = min(valley_candidates, key=lambda idx: float(df.iloc[idx]['Low']))
        valley_low = float(df.iloc[valley_idx]['Low'])
        valley_high = float(df.iloc[valley_idx]['High'])
        if breaks_below_level_after(df, top2_idx, valley_low):
            continue

        breakdown_idx = None
        reclaim_idx = None
        breakdown_ref_high = None
        for j in range(top2_idx + 1, min(len(df) - 1, top2_idx + 46)):
            low_j = float(df.iloc[j]['Low'])
            close_j = float(df.iloc[j]['Close'])
            if low_j > valley_low * 0.997:
                continue
            prev_body_low = min(float(df.iloc[j - 1]['Open']), float(df.iloc[j - 1]['Close']))
            prev_body_high = max(float(df.iloc[j - 1]['Open']), float(df.iloc[j - 1]['Close']))
            for r in range(j, min(len(df), j + 6)):
                close_r = float(df.iloc[r]['Close'])
                if close_r >= prev_body_low or close_r >= valley_low:
                    breakdown_idx = j
                    reclaim_idx = r
                    breakdown_ref_high = prev_body_high
                    break
            if breakdown_idx is not None:
                break
        if breakdown_idx is None or reclaim_idx is None:
            fallback_candidate = build_long_pullback_after_double_top(symbol, df, top1_idx, top2_idx, valley_idx)
            if fallback_candidate is not None:
                candidates.append(fallback_candidate)
            continue

        confirm_idx = None
        trendline_break = None
        rise_start = reclaim_idx
        rise_low = min(float(df.iloc[breakdown_idx]['Low']), float(df.iloc[reclaim_idx]['Low']))
        for j in range(reclaim_idx + 2, min(len(df) - 3, reclaim_idx + 31)):
            close_j = float(df.iloc[j]['Close'])
            high_j = float(df.iloc[j]['High'])
            rise_pct = (high_j / rise_low - 1.0) * 100.0 if rise_low > 0 else 0.0
            if rise_pct < 6.0:
                continue
            trendline_break = find_recent_desc_trendline_break(df, j, lookback=SHORT_TREND_LOOKBACK, window=SWING_WINDOW)
            if trendline_break is None:
                continue
            confirm_idx = j
            break
        if confirm_idx is None:
            continue
        if confirm_idx < len(df) - 35:
            continue

        long_term_trend_break = find_recent_desc_trendline_break(df, confirm_idx, lookback=LONG_TREND_LOOKBACK, window=SWING_WINDOW)

        breakout_leg_low = rise_low
        breakout_leg_high = float(df.iloc[confirm_idx]['High'])
        if breakout_leg_high <= breakout_leg_low:
            continue
        fib50 = breakout_leg_high - 0.5 * (breakout_leg_high - breakout_leg_low)
        fib618 = breakout_leg_high - 0.618 * (breakout_leg_high - breakout_leg_low)
        if fib50 < fib618:
            fib50, fib618 = fib618, fib50

        best_pullback_idx = None
        best_score = -1e9
        best_zone_text = f"雙頂壓力 {double_top_mid:.2f}"
        best_volume_feature = '一般'
        best_slowdown_feature = '一般'
        best_entry = None
        best_stop = None
        best_target1 = None
        best_target2 = None
        best_band_mid = None
        best_chip_zone_low = None
        best_chip_zone_high = None
        best_support_flags = {}
        pullback_candidates = []

        chip_zone = find_chip_dense_zone(df, confirm_idx, lookback=30)
        platform_zone = find_platform_zone(df['Close'], confirm_idx, direction='long')
        breakout_vol20 = trailing_avg_dollar_volume(df, confirm_idx, days=20) or 0.0
        breakout_day_dv = float(df.iloc[confirm_idx]['Close']) * float(df.iloc[confirm_idx]['Volume'])
        breakout_volume_feature = '確認放量' if breakout_vol20 > 0 and breakout_day_dv > breakout_vol20 else '確認量平'

        prior_high_idx = nearest_swing_high(df, reclaim_idx, max(reclaim_idx + 1, confirm_idx - 1))
        prior_high = float(df.iloc[prior_high_idx]['High']) if prior_high_idx is not None else None
        support_line = trendline_break['line_value'] if trendline_break is not None else None

        post_confirm_high = breakout_leg_high
        for k in range(confirm_idx + 2, len(df) - 1):
            low = float(df.iloc[k]['Low'])
            close = float(df.iloc[k]['Close'])
            high = float(df.iloc[k]['High'])
            post_confirm_high = max(post_confirm_high, high)

            in_fib_zone = fib618 * 0.995 <= low <= fib50 * 1.002
            fib_ok, fib_reclaimed = qualifies_reclaim_after_fib_break_long(df, fib618, k, max_days=5)
            if not in_fib_zone and not fib_reclaimed:
                continue
            if not fib_ok:
                continue
            if low <= breakout_leg_low * 1.002:
                continue

            touch_chip = False
            chip_mid = None
            chip_low = None
            chip_high = None
            if chip_zone:
                chip_low, chip_high, chip_mid = chip_zone
                touch_chip = (chip_low * 0.99 <= low <= chip_high * 1.01) or (chip_low * 0.99 <= close <= chip_high * 1.01)

            touch_platform = False
            platform_low = None
            platform_high = None
            if platform_zone:
                platform_low, platform_high = platform_zone
                touch_platform = (platform_low * 0.992 <= low <= platform_high * 1.008) or (platform_low * 0.992 <= close <= platform_high * 1.008)

            touch_prior_high = bool(prior_high is not None and abs(low / prior_high - 1.0) <= 0.02)
            touch_support_line = bool(support_line and close >= support_line * 0.995)
            above_20d_close = False
            close_20d_ago = None
            if k >= 20:
                close_20d_ago = float(df.iloc[k - 20]['Close'])
                above_20d_close = close > close_20d_ago * 1.01
            support_count = sum(1 for flag in [touch_chip, touch_platform, touch_prior_high, touch_support_line] if flag)
            if support_count == 0:
                continue

            if not (low <= float(df.iloc[k-1]['Low']) and low <= float(df.iloc[k+1]['Low'])):
                continue

            rebound_idx = None
            for j in range(k + 1, min(len(df), k + 6)):
                if float(df.iloc[j]['Close']) > float(df.iloc[j-1]['High']):
                    rebound_idx = j
                    break
            daily_rebound_confirmed = rebound_idx is not None
            needs_intraday_reversal = rebound_idx is None

            vol20 = trailing_avg_dollar_volume(df, k, days=20) or 0.0
            pullback_day_dv = float(df.iloc[k]['Close']) * float(df.iloc[k]['Volume'])
            vol_shrink = vol20 > 0 and pullback_day_dv < vol20

            rise_seg = df.iloc[max(breakdown_idx, confirm_idx - 8):confirm_idx + 1]
            pb_seg = df.iloc[confirm_idx + 1:k + 1]
            slowdown = 0
            if len(rise_seg) >= 3 and len(pb_seg) >= 2:
                if avg_body(pb_seg) < avg_body(rise_seg) * 0.85:
                    slowdown += 1
                if avg_tr(pb_seg) < avg_tr(rise_seg) * 0.90:
                    slowdown += 1

            bonus = 0.0
            bonus += 10 if touch_chip else 0       # 籌碼密集區 +10 (原 +14)
            bonus += 6 if touch_platform else 0    # 平台區 +6 (原 +8)
            bonus += 4 if touch_prior_high else 0  # 前高 +4 (原 +6)
            bonus += 4 if touch_support_line else 0 # 支撐線 +4 (原 +4)
            bonus += 5 if above_20d_close else 0
            bonus += 6 if touch_chip and touch_platform else 0
            bonus += 8 if in_fib_zone else 0       # Fib 區間 +8 (原 +5)
            bonus += 4 if fib_reclaimed else 0
            bonus += 6 if vol_shrink else 0
            bonus += slowdown * 4
            bonus += min(support_count, 4) * 3
            if daily_rebound_confirmed and rebound_idx - k <= 2:
                bonus += 5
            # 長期跨度加分：每 10 日 +1 分，上限 15 分
            long_span_bonus = min((double_top_gap // 10) * LONG_SPAN_BONUS_PER_10D, LONG_SPAN_BONUS_MAX)
            bonus += long_span_bonus
            if needs_intraday_reversal:
                bonus -= 3

            entry_anchor_idx = rebound_idx if rebound_idx is not None else k
            entry_price = max(float(df.iloc[entry_anchor_idx]['Close']), float(df.iloc[k]['High']))
            stop_price = None
            if touch_chip and chip_low is not None:
                stop_price = chip_low * 0.985
            elif platform_low is not None:
                stop_price = platform_low * 0.985
            elif prior_high is not None and low > prior_high * 0.985:
                stop_price = prior_high * 0.985
            else:
                structural_low = min(low, breakout_leg_low)
                if structural_low < entry_price * 0.985:
                    stop_price = structural_low * 0.985
                else:
                    stop_price = entry_price * 0.95
            risk = entry_price - stop_price
            if risk <= 0:
                continue

            target1 = double_top_mid
            if target1 <= entry_price:
                target1 = max(double_top_mid, post_confirm_high, entry_price + max(risk * 1.2, entry_price * 0.03))
            target2 = low + 1.618 * (breakout_leg_high - breakout_leg_low)
            if target2 <= target1:
                target2 = max(target1 + risk, entry_price + 1.618 * risk)
            rr1 = (target1 - entry_price) / risk if target1 > entry_price else 0.0
            bonus += min(rr1, 4.0) * 6

            pullback_candidates.append({'idx': k, 'price_level': low})
            qualifying_pullbacks_all.append({'idx': k, 'price_level': low})

            if bonus > best_score or (math.isclose(bonus, best_score) and (best_pullback_idx is None or k > best_pullback_idx)):
                best_score = bonus
                best_pullback_idx = k
                best_entry = entry_price
                best_stop = stop_price
                best_target1 = target1
                best_target2 = target2
                best_band_mid = chip_mid
                best_chip_zone_low = chip_low
                best_chip_zone_high = chip_high
                zone_parts = [f"回踩 0.5-0.618 ({fib50:.2f}/{fib618:.2f})"]
                if fib_reclaimed and not in_fib_zone:
                    zone_parts.append('跌穿0.618後5日內收回')
                if touch_chip and chip_mid is not None:
                    zone_parts.append(f"籌碼密集區 {chip_low:.2f}-{chip_high:.2f} / 中軸 {chip_mid:.2f}")
                if touch_platform and platform_low is not None and platform_high is not None:
                    zone_parts.append(f"平台區 {platform_low:.2f}-{platform_high:.2f}")
                if touch_prior_high and prior_high is not None:
                    zone_parts.append(f"前高支撐 {prior_high:.2f}")
                if above_20d_close and close_20d_ago is not None:
                    zone_parts.append(f"回調價高於20日前收市 {close_20d_ago:.2f}")
                best_zone_text = ' / '.join(zone_parts)
                best_volume_feature = '確認放量+回調量縮' if breakout_volume_feature == '確認放量' and vol_shrink else (breakout_volume_feature if not vol_shrink else '回調量縮')
                best_slowdown_feature = '回調減速' if slowdown >= 1 else '回調正常'
                best_support_flags = {
                    'touch_chip': touch_chip,
                    'touch_platform': touch_platform,
                    'touch_prior_high': touch_prior_high,
                    'touch_support_line': touch_support_line,
                    'above_20d_close': above_20d_close,
                    'close_20d_ago': round(float(close_20d_ago), 2) if close_20d_ago is not None else None,
                    'support_count': support_count,
                    'fib_reclaimed': fib_reclaimed,
                }

        if best_pullback_idx is None:
            continue

        recent_windows = build_recent_windows(df, pullback_candidates, bullish=True, max_windows=3, max_gap_days=3)
        recent_windows = filter_recent_windows_by_direction(df, recent_windows, bullish=True, days=20, min_pct=1.0)
        if recent_windows:
            filtered_pullback = date_to_index(df, recent_windows[-1]['representative_date'])
            if filtered_pullback is not None:
                best_pullback_idx = filtered_pullback

        score = 50.0
        score += max(0.0, 10.0 - max(0, double_top_gap - 20) * 0.15)
        score += max(0.0, 12.0 - pct_diff(top1_high, top2_high) * 800.0)
        score += score_confirm_day(df, confirm_idx, bullish=True)
        score += best_score
        if long_term_trend_break is not None:
            score += LONG_TERM_TREND_BONUS
        if double_top_gap >= 60:
            score += LONG_SPAN_BONUS

        logic = '先找兩個相隔至少20日、頂價差2%內的明顯雙頂；雙頂之間不可出現更高價，第二頂後不可再跌破谷底；其後等待結構確認與趨勢線突破，最後只做回踩0.5-0.618且有籌碼密集區/平台/前高/支撐線共振的第二買點；回調後可用日線重新轉強或同日30m反轉確認。'
        candidate = make_result(
            symbol=symbol,
            direction='做多',
            pattern='雙頂→破底翻→回調買點',
            zone=best_zone_text,
            event_date=df.iloc[breakdown_idx]['Date'],
            confirm_date=df.iloc[confirm_idx]['Date'],
            pullback_date=df.iloc[best_pullback_idx]['Date'],
            price=df.iloc[-1]['Close'],
            fib618=fib618,
            volume_feature=best_volume_feature,
            slowdown_feature=best_slowdown_feature,
            score=score,
            logic=logic,
            recent_windows=recent_windows,
        )
        candidate.update({
            'entry_price': round(float(best_entry), 2) if best_entry is not None else None,
            'double_top_1_date': df.iloc[top1_idx]['Date'].strftime('%Y-%m-%d'),
            'double_top_2_date': df.iloc[top2_idx]['Date'].strftime('%Y-%m-%d'),
            'double_top_mid': round(float(double_top_mid), 2),
            'double_top_gap_days': int(double_top_gap),
            'valley_date': df.iloc[valley_idx]['Date'].strftime('%Y-%m-%d'),
            'valley_low': round(float(valley_low), 2),
            'valley_high': round(float(valley_high), 2),
            'breakdown_date': df.iloc[breakdown_idx]['Date'].strftime('%Y-%m-%d'),
            'breakdown_reclaim_date': df.iloc[reclaim_idx]['Date'].strftime('%Y-%m-%d'),
            'breakdown_ref_high': round(float(breakdown_ref_high), 2) if breakdown_ref_high is not None else None,
            'trend_break_date': df.iloc[confirm_idx]['Date'].strftime('%Y-%m-%d'),
            'chip_zone_low': round(float(best_chip_zone_low), 2) if best_chip_zone_low is not None else None,
            'chip_zone_high': round(float(best_chip_zone_high), 2) if best_chip_zone_high is not None else None,
            'chip_zone_mid': round(float(best_band_mid), 2) if best_band_mid is not None else None,
            'support_flags': best_support_flags,
            'risk_reward_1': round(float((best_target1 - best_entry) / (best_entry - best_stop)), 2) if best_entry is not None and best_stop is not None and best_target1 is not None and best_entry > best_stop else None,
            'daily_rebound_confirmed': daily_rebound_confirmed,
            'needs_intraday_reversal': needs_intraday_reversal,
        })
        candidates.append(candidate)

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x['score'], x['_sort_pullback'], x['_sort_confirm']), reverse=True)
    best = candidates[0]
    best['recent_windows'] = filter_recent_windows_by_direction(
        df,
        build_recent_windows(df, qualifying_pullbacks_all, bullish=True, max_windows=3, max_gap_days=3),
        bullish=True,
        days=20,
        min_pct=1.0,
    )
    return best


def scan_short(symbol, df):
    if len(df) < 90:
        return None
    df = df.copy().dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume']).reset_index(drop=True)
    if len(df) < 90:
        return None

    pivot_window = SWING_WINDOW
    pivot_lows = []
    pivot_highs = []
    for i in range(pivot_window, len(df) - pivot_window):
        low_seg = df['Low'].iloc[i-pivot_window:i+pivot_window+1]
        high_seg = df['High'].iloc[i-pivot_window:i+pivot_window+1]
        if float(df.iloc[i]['Low']) == float(low_seg.min()) and int(low_seg.argmin()) == pivot_window:
            pivot_lows.append(i)
        if float(df.iloc[i]['High']) == float(high_seg.max()) and int(high_seg.argmax()) == pivot_window:
            pivot_highs.append(i)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None

    candidates = []
    qualifying_pullbacks_all = []

    for low2_idx in pivot_lows:
        if low2_idx < 30 or low2_idx > len(df) - 10:
            continue
        low2_price = float(df.iloc[low2_idx]['Low'])

        low1_candidates = [
            idx for idx in pivot_lows
            if low2_idx - 80 <= idx <= low2_idx - 20
            and pct_diff(float(df.iloc[idx]['Low']), low2_price) <= 0.02
        ]
        if not low1_candidates:
            continue
        low1_idx = min(low1_candidates, key=lambda idx: abs((low2_idx - idx) - 15))
        low1_price = float(df.iloc[low1_idx]['Low'])
        if has_lower_low_between(df, low1_idx, low2_idx, min(low1_price, low2_price)):
            continue
        double_bottom_mid = (low1_price + low2_price) / 2.0
        double_bottom_gap = low2_idx - low1_idx

        peak_candidates = [idx for idx in pivot_highs if low1_idx + 2 <= idx <= low2_idx - 2]
        if not peak_candidates:
            continue
        peak_idx = max(peak_candidates, key=lambda idx: float(df.iloc[idx]['High']))
        peak_high = float(df.iloc[peak_idx]['High'])
        peak_low = float(df.iloc[peak_idx]['Low'])
        if breaks_above_level_after(df, low2_idx, peak_high):
            continue

        breakout_idx = None
        reclaim_idx = None
        breakout_ref_low = None
        for j in range(low2_idx + 1, min(len(df) - 1, low2_idx + 46)):
            high_j = float(df.iloc[j]['High'])
            close_j = float(df.iloc[j]['Close'])
            if high_j < peak_high * 1.003:
                continue
            prev_body_low = min(float(df.iloc[j - 1]['Open']), float(df.iloc[j - 1]['Close']))
            prev_body_high = max(float(df.iloc[j - 1]['Open']), float(df.iloc[j - 1]['Close']))
            for r in range(j, min(len(df), j + 6)):
                close_r = float(df.iloc[r]['Close'])
                if close_r <= prev_body_high or close_r <= peak_high:
                    breakout_idx = j
                    reclaim_idx = r
                    breakout_ref_low = prev_body_low
                    break
            if breakout_idx is not None:
                break
        if breakout_idx is None or reclaim_idx is None:
            fallback_candidate = build_short_right_shoulder_after_double_bottom(symbol, df, low1_idx, low2_idx, peak_idx)
            if fallback_candidate is not None:
                candidates.append(fallback_candidate)
            continue

        confirm_idx = None
        trendline_break = None
        drop_high = max(float(df.iloc[breakout_idx]['High']), float(df.iloc[reclaim_idx]['High']))
        for j in range(reclaim_idx + 2, min(len(df) - 3, reclaim_idx + 31)):
            close_j = float(df.iloc[j]['Close'])
            low_j = float(df.iloc[j]['Low'])
            drop_pct = (drop_high / low_j - 1.0) * 100.0 if low_j > 0 else 0.0
            if drop_pct < 6.0:
                continue
            trendline_break = find_recent_asc_trendline_break(df, j, lookback=SHORT_TREND_LOOKBACK, window=SWING_WINDOW)
            if trendline_break is None:
                continue
            confirm_idx = j
            break
        if confirm_idx is None:
            continue
        if confirm_idx < len(df) - 35:
            continue

        long_term_trend_break = find_recent_asc_trendline_break(df, confirm_idx, lookback=LONG_TREND_LOOKBACK, window=SWING_WINDOW)

        breakout_leg_high = drop_high
        breakout_leg_low = float(df.iloc[confirm_idx]['Low'])
        if breakout_leg_high <= breakout_leg_low:
            continue
        fib50 = breakout_leg_low + 0.5 * (breakout_leg_high - breakout_leg_low)
        fib618 = breakout_leg_low + 0.618 * (breakout_leg_high - breakout_leg_low)
        if fib50 > fib618:
            fib50, fib618 = fib618, fib50

        best_pullback_idx = None
        best_score = -1e9
        best_zone_text = f"雙底支撐 {double_bottom_mid:.2f}"
        best_volume_feature = '一般'
        best_slowdown_feature = '一般'
        best_entry = None
        best_stop = None
        best_target1 = None
        best_target2 = None
        best_band_mid = None
        best_chip_zone_low = None
        best_chip_zone_high = None
        best_support_flags = {}
        pullback_candidates = []

        chip_zone = find_chip_dense_zone(df, confirm_idx, lookback=30)
        platform_zone = find_platform_zone(df['Close'], confirm_idx, direction='short')
        breakout_vol20 = trailing_avg_dollar_volume(df, confirm_idx, days=20) or 0.0
        breakout_day_dv = float(df.iloc[confirm_idx]['Close']) * float(df.iloc[confirm_idx]['Volume'])
        breakout_volume_feature = '確認放量' if breakout_vol20 > 0 and breakout_day_dv > breakout_vol20 else '確認量平'

        prior_low_idx = nearest_swing_low(df, reclaim_idx, max(reclaim_idx + 1, confirm_idx - 1))
        prior_low = float(df.iloc[prior_low_idx]['Low']) if prior_low_idx is not None else None
        resistance_line = trendline_break['line_value'] if trendline_break is not None else None

        post_confirm_low = breakout_leg_low
        for k in range(confirm_idx + 2, len(df) - 1):
            low = float(df.iloc[k]['Low'])
            close = float(df.iloc[k]['Close'])
            high = float(df.iloc[k]['High'])
            post_confirm_low = min(post_confirm_low, low)

            in_fib_zone = fib50 * 0.99 <= high <= fib618 * 1.005
            fib_ok, fib_reclaimed = qualifies_reclaim_after_fib_break_short(df, fib618, k, max_days=5)
            if not in_fib_zone and not fib_reclaimed:
                continue
            if not fib_ok:
                continue
            if high >= breakout_leg_high * 0.998:
                continue

            touch_chip = False
            chip_mid = None
            chip_low = None
            chip_high = None
            if chip_zone:
                chip_low, chip_high, chip_mid = chip_zone
                touch_chip = (chip_low * 0.99 <= high <= chip_high * 1.01) or (chip_low * 0.99 <= close <= chip_high * 1.01)

            touch_platform = False
            platform_low = None
            platform_high = None
            if platform_zone:
                platform_low, platform_high = platform_zone
                touch_platform = (platform_low * 0.992 <= high <= platform_high * 1.008) or (platform_low * 0.992 <= close <= platform_high * 1.008)

            touch_prior_low = bool(prior_low is not None and abs(high / prior_low - 1.0) <= 0.02)
            touch_resistance_line = bool(resistance_line and close <= resistance_line * 1.005)
            below_20d_close = False
            close_20d_ago = None
            if k >= 20:
                close_20d_ago = float(df.iloc[k - 20]['Close'])
                below_20d_close = close < close_20d_ago * 0.99
            support_count = sum(1 for flag in [touch_chip, touch_platform, touch_prior_low, touch_resistance_line] if flag)
            if support_count == 0:
                continue

            if not (high >= float(df.iloc[k-1]['High']) and high >= float(df.iloc[k+1]['High'])):
                continue

            breakdown_follow_idx = None
            for j in range(k + 1, min(len(df), k + 6)):
                if float(df.iloc[j]['Close']) < float(df.iloc[j-1]['Low']):
                    breakdown_follow_idx = j
                    break
            daily_breakdown_confirmed = breakdown_follow_idx is not None
            needs_intraday_reversal = breakdown_follow_idx is None

            vol20 = trailing_avg_dollar_volume(df, k, days=20) or 0.0
            pullback_day_dv = float(df.iloc[k]['Close']) * float(df.iloc[k]['Volume'])
            vol_shrink = vol20 > 0 and pullback_day_dv < vol20

            drop_seg = df.iloc[max(breakout_idx, confirm_idx - 8):confirm_idx + 1]
            pb_seg = df.iloc[confirm_idx + 1:k + 1]
            slowdown = 0
            if len(drop_seg) >= 3 and len(pb_seg) >= 2:
                if avg_body(pb_seg) < avg_body(drop_seg) * 0.85:
                    slowdown += 1
                if avg_tr(pb_seg) < avg_tr(drop_seg) * 0.90:
                    slowdown += 1

            bonus = 0.0
            bonus += 10 if touch_chip else 0       # 籌碼密集區 +10 (原 +14)
            bonus += 6 if touch_platform else 0    # 平台區 +6 (原 +8)
            bonus += 4 if touch_prior_low else 0   # 前低 +4 (原 +6)
            bonus += 4 if touch_resistance_line else 0  # 阻力線 +4
            bonus += 5 if below_20d_close else 0
            bonus += 6 if touch_chip and touch_platform else 0
            bonus += 8 if in_fib_zone else 0       # Fib 區間 +8 (原 +5)
            bonus += 4 if fib_reclaimed else 0
            bonus += 6 if vol_shrink else 0
            bonus += slowdown * 4
            bonus += min(support_count, 4) * 3
            if daily_breakdown_confirmed and breakdown_follow_idx - k <= 2:
                bonus += 5
            # 長期跨度加分：每 10 日 +1 分，上限 15 分
            long_span_bonus = min((double_bottom_gap // 10) * LONG_SPAN_BONUS_PER_10D, LONG_SPAN_BONUS_MAX)
            bonus += long_span_bonus
            if needs_intraday_reversal:
                bonus -= 3

            entry_anchor_idx = breakdown_follow_idx if breakdown_follow_idx is not None else k
            entry_price = min(float(df.iloc[entry_anchor_idx]['Close']), float(df.iloc[k]['Low']))
            stop_price = None
            if touch_chip and chip_high is not None:
                stop_price = chip_high * 1.015
            elif platform_high is not None:
                stop_price = platform_high * 1.015
            elif prior_low is not None and high < prior_low * 1.015:
                stop_price = prior_low * 1.015
            else:
                structural_high = max(high, breakout_leg_high)
                if structural_high > entry_price * 1.015:
                    stop_price = structural_high * 1.015
                else:
                    stop_price = entry_price * 1.05
            risk = stop_price - entry_price
            if risk <= 0:
                continue

            target1 = double_bottom_mid
            if target1 >= entry_price:
                target1 = min(double_bottom_mid, post_confirm_low, entry_price - max(risk * 1.2, entry_price * 0.03))
            target2 = high - 1.618 * (breakout_leg_high - breakout_leg_low)
            if target2 >= target1:
                target2 = min(target1 - risk, entry_price - 1.618 * risk)
            rr1 = (entry_price - target1) / risk if target1 < entry_price else 0.0
            bonus += min(rr1, 4.0) * 6

            pullback_candidates.append({'idx': k, 'price_level': high})
            qualifying_pullbacks_all.append({'idx': k, 'price_level': high})

            if bonus > best_score or (math.isclose(bonus, best_score) and (best_pullback_idx is None or k > best_pullback_idx)):
                best_score = bonus
                best_pullback_idx = k
                best_entry = entry_price
                best_stop = stop_price
                best_target1 = target1
                best_target2 = target2
                best_band_mid = chip_mid
                best_chip_zone_low = chip_low
                best_chip_zone_high = chip_high
                zone_parts = [f"回抽 0.5-0.618 ({fib50:.2f}/{fib618:.2f})"]
                if fib_reclaimed and not in_fib_zone:
                    zone_parts.append('升穿0.618後5日內跌回')
                if touch_chip and chip_mid is not None:
                    zone_parts.append(f"籌碼密集區 {chip_low:.2f}-{chip_high:.2f} / 中軸 {chip_mid:.2f}")
                if touch_platform and platform_low is not None and platform_high is not None:
                    zone_parts.append(f"平台區 {platform_low:.2f}-{platform_high:.2f}")
                if touch_prior_low and prior_low is not None:
                    zone_parts.append(f"前低阻力 {prior_low:.2f}")
                if below_20d_close and close_20d_ago is not None:
                    zone_parts.append(f"回抽價低於20日前收市 {close_20d_ago:.2f}")
                best_zone_text = ' / '.join(zone_parts)
                best_volume_feature = '確認放量+回抽量縮' if breakout_volume_feature == '確認放量' and vol_shrink else (breakout_volume_feature if not vol_shrink else '回抽量縮')
                best_slowdown_feature = '回抽減速' if slowdown >= 1 else '回抽正常'
                best_support_flags = {
                    'touch_chip': touch_chip,
                    'touch_platform': touch_platform,
                    'touch_prior_low': touch_prior_low,
                    'touch_resistance_line': touch_resistance_line,
                    'below_20d_close': below_20d_close,
                    'close_20d_ago': round(float(close_20d_ago), 2) if close_20d_ago is not None else None,
                    'support_count': support_count,
                    'fib_reclaimed': fib_reclaimed,
                }

        if best_pullback_idx is None:
            continue

        recent_windows = build_recent_windows(df, pullback_candidates, bullish=False, max_windows=3, max_gap_days=3)
        recent_windows = filter_recent_windows_by_direction(df, recent_windows, bullish=False, days=20, min_pct=1.0)
        if recent_windows:
            filtered_pullback = date_to_index(df, recent_windows[-1]['representative_date'])
            if filtered_pullback is not None:
                best_pullback_idx = filtered_pullback

        score = 50.0
        score += max(0.0, 10.0 - max(0, double_bottom_gap - 20) * 0.15)
        score += max(0.0, 12.0 - pct_diff(low1_price, low2_price) * 800.0)
        score += score_confirm_day(df, confirm_idx, bullish=False)
        score += best_score
        if long_term_trend_break is not None:
            score += LONG_TERM_TREND_BONUS
        if double_bottom_gap >= 60:
            score += LONG_SPAN_BONUS

        logic = '先找兩個相隔至少20日、底價差2%內的明顯雙底；雙底之間不可出現更低價，第二底後不可再升破峰頂；其後等待結構確認與跌破近期上升趨勢線，最後只做回抽0.5-0.618且有籌碼密集區/平台/前低/阻力線共振的第二賣點；回抽後可用日線重新轉弱或同日30m反轉確認。'
        candidate = make_result(
            symbol=symbol,
            direction='做空',
            pattern='雙底→假突破→回調賣點',
            zone=best_zone_text,
            event_date=df.iloc[breakout_idx]['Date'],
            confirm_date=df.iloc[confirm_idx]['Date'],
            pullback_date=df.iloc[best_pullback_idx]['Date'],
            price=df.iloc[-1]['Close'],
            fib618=fib618,
            volume_feature=best_volume_feature,
            slowdown_feature=best_slowdown_feature,
            score=score,
            logic=logic,
            recent_windows=recent_windows,
        )
        candidate.update({
            'entry_price': round(float(best_entry), 2) if best_entry is not None else None,
            'double_bottom_1_date': df.iloc[low1_idx]['Date'].strftime('%Y-%m-%d'),
            'double_bottom_2_date': df.iloc[low2_idx]['Date'].strftime('%Y-%m-%d'),
            'double_bottom_mid': round(float(double_bottom_mid), 2),
            'double_bottom_gap_days': int(double_bottom_gap),
            'peak_date': df.iloc[peak_idx]['Date'].strftime('%Y-%m-%d'),
            'peak_high': round(float(peak_high), 2),
            'peak_low': round(float(peak_low), 2),
            'breakout_date': df.iloc[breakout_idx]['Date'].strftime('%Y-%m-%d'),
            'fake_breakdown_date': df.iloc[reclaim_idx]['Date'].strftime('%Y-%m-%d'),
            'breakout_ref_low': round(float(breakout_ref_low), 2) if breakout_ref_low is not None else None,
            'trend_break_date': df.iloc[confirm_idx]['Date'].strftime('%Y-%m-%d'),
            'chip_zone_low': round(float(best_chip_zone_low), 2) if best_chip_zone_low is not None else None,
            'chip_zone_high': round(float(best_chip_zone_high), 2) if best_chip_zone_high is not None else None,
            'chip_zone_mid': round(float(best_band_mid), 2) if best_band_mid is not None else None,
            'support_flags': best_support_flags,
            'risk_reward_1': round(float((best_entry - best_target1) / (best_stop - best_entry)), 2) if best_entry is not None and best_stop is not None and best_target1 is not None and best_stop > best_entry else None,
            'daily_breakdown_confirmed': daily_breakdown_confirmed,
            'needs_intraday_reversal': needs_intraday_reversal,
        })
        candidates.append(candidate)

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x['score'], x['_sort_pullback'], x['_sort_confirm']), reverse=True)
    best = candidates[0]
    best['recent_windows'] = filter_recent_windows_by_direction(
        df,
        build_recent_windows(df, qualifying_pullbacks_all, bullish=False, max_windows=3, max_gap_days=3),
        bullish=False,
        days=20,
        min_pct=1.0,
    )
    return best


def main():
    parser = argparse.ArgumentParser(description='U.S. double-top breakdown-reclaim pullback scan')
    parser.add_argument('--format', choices=['json', 'markdown'], default='json')
    parser.add_argument('--max-symbols', type=int, default=0, help='Optional cap on universe size for smoke tests')
    parser.add_argument('--stderr-path', default='/tmp/us_pattern_scan_yf_stderr.log')
    parser.add_argument('--shards', type=int, default=int(os.environ.get('HERMES_SCAN_SHARDS', '4')), help='Number of internal stage2 shards')
    parser.add_argument('--artifact-dir', default=os.environ.get('HERMES_SCAN_ARTIFACT_DIR', ''), help='Optional directory for shard artifacts')
    parser.add_argument('--stage1-period', default=os.environ.get('HERMES_SCAN_STAGE1_PERIOD', '1mo'), help='Short lookback window used for liquidity screening')
    parser.add_argument('--stage1-batch', type=int, default=int(os.environ.get('HERMES_SCAN_STAGE1_BATCH', '90')), help='Batch size for stage1 liquidity download')
    parser.add_argument('--stage2-batch', type=int, default=int(os.environ.get('HERMES_SCAN_STAGE2_BATCH', '120')), help='Batch size for stage2 deep-history download')
    args = parser.parse_args()

    stderr_path = args.stderr_path
    open(stderr_path, 'w').close()
    artifact_dir = Path(args.artifact_dir).expanduser() if args.artifact_dir else Path(stderr_path).resolve().parent / (Path(stderr_path).stem + '.artifacts')
    artifact_dir.mkdir(parents=True, exist_ok=True)

    append_log(
        stderr_path,
        f"SCAN_START format={args.format} max_symbols={args.max_symbols or 'all'} shards={max(1, args.shards)} stage1_period={args.stage1_period} stage1_batch={args.stage1_batch} stage2_batch={args.stage2_batch}"
    )
    nasdaq = parse_nasdaq_listed(fetch_text(NASDAQ_LISTED_URL))
    other = parse_other_listed(fetch_text(OTHER_LISTED_URL))
    uni = pd.concat([nasdaq, other], ignore_index=True)
    uni = uni.drop_duplicates(subset=['Symbol']).reset_index(drop=True)
    uni['keep'] = uni.apply(lambda r: is_regular_security(r['Symbol'], r['name'], bool(r['etf']), bool(r['test_issue'])), axis=1)
    uni = uni[uni['keep']].copy()
    if args.max_symbols and args.max_symbols > 0:
        uni = uni.head(args.max_symbols).copy()
    original_symbols = uni['Symbol'].tolist()
    mapped = {yahoo_symbol(sym): sym for sym in original_symbols}
    yahoo_symbols = list(mapped.keys())
    append_log(stderr_path, f"STAGE1_START universe={len(yahoo_symbols)}")

    stage1, miss1 = download_bars(yahoo_symbols, args.stage1_period, stderr_path, batch=args.stage1_batch, phase='STAGE1')
    liquid = []
    for ys, df in stage1.items():
        x = df.dropna(subset=['Close', 'Volume']).reset_index(drop=True)
        if len(x) == 0:
            continue
        avg_dollar_vol_20d = trailing_avg_dollar_volume(x, len(x) - 1, days=20)
        if avg_dollar_vol_20d is not None and avg_dollar_vol_20d >= 20_000_000:
            liquid.append(ys)
    append_log(stderr_path, f"STAGE1_DONE ok={len(stage1)} liquid={len(liquid)} misses={len(miss1)}")

    (artifact_dir / 'liquid_symbols.json').write_text(
        json.dumps({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'universe_total': len(yahoo_symbols),
            'liquid_count': len(liquid),
            'liquid_symbols': liquid,
        }, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    shard_lists = split_into_shards(liquid, max(1, args.shards))
    results = []
    long_count = 0
    short_count = 0
    deep_scan_count = 0
    miss2 = set()
    shard_summaries = []

    for shard_idx, shard_symbols in enumerate(shard_lists, start=1):
        append_log(stderr_path, f"STAGE2_SHARD_START shard={shard_idx}/{len(shard_lists)} symbols={len(shard_symbols)}")
        stage2, shard_miss = download_bars(shard_symbols, '8mo', stderr_path, batch=args.stage2_batch, phase=f'STAGE2_SHARD_{shard_idx:02d}')
        shard_results, shard_long, shard_short = scan_stage2_dataset(stage2, mapped, stderr_path)
        deep_scan_count += len(stage2)
        miss2.update(shard_miss)
        results.extend(shard_results)
        long_count += shard_long
        short_count += shard_short
        shard_summary = {
            'shard': shard_idx,
            'input_symbols': len(shard_symbols),
            'downloaded_symbols': len(stage2),
            'misses': len(shard_miss),
            'candidates': len(shard_results),
            'long_candidates': shard_long,
            'short_candidates': shard_short,
        }
        shard_summaries.append(shard_summary)
        shard_path = artifact_dir / f'shard_{shard_idx:02d}.json'
        shard_path.write_text(
            json.dumps({
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'summary': shard_summary,
                'results': shard_results,
                'miss_symbols': sorted(list(shard_miss)),
            }, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        append_log(
            stderr_path,
            f"STAGE2_SHARD_DONE shard={shard_idx}/{len(shard_lists)} downloaded={len(stage2)} misses={len(shard_miss)} candidates={len(shard_results)}"
        )

    enrich_rows_with_intraday_30m(results, stderr_path)
    results.sort(key=lambda x: (x['_sort_pullback'], x.get('intraday_30m_priority', 0), x['score'], x['_sort_event'], x['_sort_confirm']), reverse=True)
    deduped = []
    seen_symbols = set()
    for row in results:
        if row['symbol'] in seen_symbols:
            continue
        deduped.append(row)
        seen_symbols.add(row['symbol'])
    top10 = deduped[:10]
    top10_long = [row for row in deduped if row['direction'] == '做多'][:10]
    top10_short = [row for row in deduped if row['direction'] == '做空'][:10]

    band_rows_20m_to_50m = []
    band_rows_50m_plus = []
    for row in deduped:
        row_20m_to_50m = clone_row_for_liquidity_band(row, '20m_to_50m')
        row_50m_plus = clone_row_for_liquidity_band(row, '50m_plus')
        if row_20m_to_50m:
            band_rows_20m_to_50m.append(row_20m_to_50m)
        if row_50m_plus:
            band_rows_50m_plus.append(row_50m_plus)

    band_rows_20m_to_50m.sort(key=lambda x: (x['_sort_pullback'], x.get('intraday_30m_priority', 0), x['score'], x['_sort_event'], x['_sort_confirm']), reverse=True)
    band_rows_50m_plus.sort(key=lambda x: (x['_sort_pullback'], x.get('intraday_30m_priority', 0), x['score'], x['_sort_event'], x['_sort_confirm']), reverse=True)

    top10_long_20m_to_50m = [row for row in band_rows_20m_to_50m if row['direction'] == '做多'][:10]
    top10_short_20m_to_50m = [row for row in band_rows_20m_to_50m if row['direction'] == '做空'][:10]
    top10_long_50m_plus = [row for row in band_rows_50m_plus if row['direction'] == '做多'][:10]
    top10_short_50m_plus = [row for row in band_rows_50m_plus if row['direction'] == '做空'][:10]

    out = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'data_sources': [
            'Nasdaq Trader nasdaqlisted.txt',
            'Nasdaq Trader otherlisted.txt',
            'Yahoo Finance / yfinance 日线 OHLCV',
            'Yahoo Finance / yfinance 30分鐘 OHLCV（盤中 30m 反應優先排序）',
            '回調日過去20個交易日平均交易額分組（2000萬-5000萬美元；5000萬美元以上）',
        ],
        'universe_total': int(len(original_symbols)),
        'liquid_count': int(len(liquid)),
        'deep_scan_count': int(deep_scan_count),
        'stage1_misses': int(len(miss1)),
        'stage2_misses': int(len(miss2)),
        'candidate_total': int(len(results)),
        'long_candidates': int(long_count),
        'short_candidates': int(short_count),
        'stderr_log': stderr_path,
        'artifact_dir': str(artifact_dir),
        'shard_count': len(shard_lists),
        'shards': shard_summaries,
        'top10': top10,
        'top10_long': top10_long,
        'top10_short': top10_short,
        'top10_long_20m_to_50m': top10_long_20m_to_50m,
        'top10_short_20m_to_50m': top10_short_20m_to_50m,
        'top10_long_50m_plus': top10_long_50m_plus,
        'top10_short_50m_plus': top10_short_50m_plus,
    }
    (artifact_dir / 'final_output.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    append_log(stderr_path, f"SCAN_DONE deep_scan={deep_scan_count} candidates={len(results)} deduped={len(deduped)}")
    if args.format == 'markdown':
        print(render_markdown_report(out))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
