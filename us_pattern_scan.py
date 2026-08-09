#!/usr/bin/env python3
"""
Consolidation Base Scanner - 长期底部盘整扫描器
扫描 3年周线，寻找长期底部盘整形态（≥1年盘整）
支持：盘中、刚突破两个榜单
支持周缓存（每周增量更新，不重复扫描全量）
"""
import argparse
import json
import math
import os
import random
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import yfinance as yf

# ========== 盘整扫描专用参数 ==========
CONSOLIDATION_PERIOD = '3y'          # 下载3年数据
CONSOLIDATION_INTERVAL = '1wk'       # 周线
MIN_CONSOLIDATION_WEEKS = 52         # 至少1年（约52周）
MAX_CONSOLIDATION_WEEKS = 156        # 最多3年（约156周）
AMPLITUDE_THRESHOLD = 0.40           # 区间振幅上限 (High-Low)/Low ≤ 40%
BREAKOUT_THRESHOLD = 0.03            # 突破判定：收盘价超过上沿 3%
BREAKDOWN_REVERSAL_THRESHOLD = 0.02  # 假跌破：收盘价低于下沿 2% 后收回
AMPLITUDE_SHRINK_FACTOR = 0.8        # 振幅收窄判定：后半段≤前半段×0.8
REVERSAL_BONUS_PER = 3               # 每次破底翻 +3 分
DURATION_BONUS_PER_YEAR = 10         # 每多一年 +10 分
SHRINK_BONUS = 8                     # 振幅收窄加分
BREAKOUT_VOL_MULTIPLIER = 1.5        # 突破放量倍数
CHIP_BONUS = 5                       # 筹码密集区加分
SLOPE_BONUS_MAX = 5                  # 20周斜率最大加分
BOTTOM_THRESHOLD = 0.70              # 底部区域阈值：区间中轴 ≤ 3年最高价 × 0.70

# 缓存配置
CACHE_DIR_NAME = '.consolidation_cache'
CACHE_MAX_AGE_DAYS = 8               # 缓存有效期 8 天（周更新）

# ========== 以下复用原有基础设施 ==========
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
UA = "Mozilla/5.0 (X11; Linux x86_64) Hermes-Agent/1.0"

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
    if any(token in lname for token in [" class a", " class b", " class c", " ordinary", "common"]):
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

def download_bars(symbols, period, stderr_path, batch=200, phase='DOWNLOAD', interval='1wk',
                  start_date=None, end_date=None):
    """
    下载数据，支持period或start/end区间。
    若start_date和end_date提供，则忽略period。
    """
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
                kwargs = {
                    'tickers': tickers,
                    'interval': interval,
                    'auto_adjust': False,
                    'group_by': 'ticker',
                    'progress': False,
                    'threads': False,
                    'prepost': False,
                    'timeout': 30,
                }
                if start_date and end_date:
                    kwargs['start'] = start_date
                    kwargs['end'] = end_date
                else:
                    kwargs['period'] = period
                data = run_with_hard_timeout(
                    45,
                    lambda: yf.download(**kwargs)
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

def find_chip_dense_zone(df, around_idx, lookback=90, bins=24):
    """
    筹码密集区 - 90周半衰期衰减 + 宽度考虑
    返回: (zone_low, zone_high, peak_mid)
    """
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
    
    days_ago = np.arange(len(seg) - 1, -1, -1)
    decay_weights = 0.5 ** (days_ago / 20.0)
    
    for row_idx, (_, row) in enumerate(seg.iterrows()):
        lo = float(row['Low'])
        hi = float(row['High'])
        vol = max(float(row['Volume']), 0.0)
        if not np.isfinite(lo) or not np.isfinite(hi) or hi < lo:
            continue
        time_weight = decay_weights[row_idx]
        if hi == lo:
            idx = int(np.clip(np.searchsorted(edges, lo, side='right') - 1, 0, bins - 1))
            weights[idx] += vol * time_weight
            continue
        touched = np.where((edges[:-1] < hi) & (edges[1:] > lo))[0]
        if len(touched) == 0:
            idx = int(np.clip(np.searchsorted(edges, (lo + hi) / 2.0, side='right') - 1, 0, bins - 1))
            weights[idx] += vol * time_weight
            continue
        span = hi - lo
        for idx in touched:
            overlap = max(0.0, min(hi, edges[idx + 1]) - max(lo, edges[idx]))
            if overlap > 0:
                weights[idx] += vol * (overlap / span) * time_weight
    if float(weights.sum()) <= 0:
        return None
    peak_idx = int(np.argmax(weights))
    peak_mid = float((edges[peak_idx] + edges[peak_idx + 1]) / 2.0)
    width = max((price_high - price_low) / bins * 1.5, peak_mid * 0.006)
    return peak_mid - width, peak_mid + width, peak_mid

# ========== 核心检测函数 ==========

def detect_bottom_consolidation(df: pd.DataFrame) -> dict:
    """
    检测股票是否处于长期底部盘整状态。
    返回字典包含：
        - in_consolidation: bool
        - zone_low, zone_high: float
        - start_idx, end_idx: int
        - duration_weeks: int
        - amplitude_early, amplitude_late: float
        - reversal_count: int (破底翻次数)
        - is_breakout: bool
        - breakout_volume_ratio: float
        - latest_close: float
        - chip_zone: tuple or None
        - reversal_count_detail: int (用于输出)
    """
    if len(df) < MIN_CONSOLIDATION_WEEKS:
        return {'in_consolidation': False}
    
    # 从最新往前找最长的符合振幅阈值的区间
    max_lookback = min(len(df), MAX_CONSOLIDATION_WEEKS)
    
    best = None
    # 从最新往前，尝试不同的结束点
    for end in range(len(df)-1, len(df) - max_lookback - 1, -1):
        # 区间必须至少52周
        if end < MIN_CONSOLIDATION_WEEKS:
            break
        # 向前扩展start，直到振幅超过阈值
        for start in range(end - MIN_CONSOLIDATION_WEEKS + 1, -1, -1):
            seg = df.iloc[start:end+1]
            low = seg['Low'].min()
            high = seg['High'].max()
            amplitude = (high - low) / low if low > 0 else 999
            if amplitude <= AMPLITUDE_THRESHOLD:
                # 找到符合条件的区间，记录最长的（start最小）
                if best is None or start < best['start_idx']:
                    best = {
                        'start_idx': start,
                        'end_idx': end,
                        'duration_weeks': end - start + 1,
                        'amplitude': amplitude,
                    }
                break  # 更早的start振幅只会更大
        if best and (end - best['start_idx'] + 1) >= MIN_CONSOLIDATION_WEEKS * 2:
            break
    
    if best is None or best['duration_weeks'] < MIN_CONSOLIDATION_WEEKS:
        return {'in_consolidation': False}
    
    start_idx = best['start_idx']
    end_idx = best['end_idx']
    duration_weeks = best['duration_weeks']
    
    # ===== 硬过滤 1：盘整区间必须延伸至最新一周 =====
    if end_idx != len(df) - 1:
        return {'in_consolidation': False}
    
    # ===== 提取区间数据 =====
    zone_df = df.iloc[start_idx:end_idx+1]
    zone_low = float(zone_df['Low'].min())
    zone_high = float(zone_df['High'].max())
    zone_mid = (zone_low + zone_high) / 2.0
    latest_close = float(df.iloc[-1]['Close'])
    high3y = float(df['High'].max())
    
    # ===== 硬过滤 2：底部区域判断（区间中轴 ≤ 3年最高价 × BOTTOM_THRESHOLD） =====
    if high3y == 0 or zone_mid > high3y * BOTTOM_THRESHOLD:
        return {'in_consolidation': False}
    
    # 当前状态（仅用于返回信息）
    is_breakout = latest_close > zone_high * (1 + BREAKOUT_THRESHOLD)
    breakout_vol_ratio = 0
    if is_breakout:
        vol20 = float(df.iloc[-20:]['Volume'].mean()) if len(df) >= 20 else 1
        latest_vol = float(df.iloc[-1]['Volume'])
        if vol20 > 0:
            breakout_vol_ratio = latest_vol / vol20
    
    # 前半段/后半段振幅
    half = len(zone_df) // 2
    early = zone_df.iloc[:half]
    late = zone_df.iloc[half:]
    amp_early = (early['High'].max() - early['Low'].min()) / early['Low'].min() if len(early) > 0 else 0
    amp_late = (late['High'].max() - late['Low'].min()) / late['Low'].min() if len(late) > 0 else 0
    
    # 破底翻次数：收盘价跌破下沿2%后，后续2周内收回
    reversal_count = 0
    for i in range(start_idx, end_idx):
        if i+2 >= len(df):
            break
        close = float(df.iloc[i]['Close'])
        if close < zone_low * (1 - BREAKDOWN_REVERSAL_THRESHOLD):
            # 检查后续2周是否收回
            if any(df.iloc[j]['Close'] > zone_low for j in range(i+1, min(i+3, len(df)))):
                reversal_count += 1
    
    # 筹码密集区
    chip_zone = None
    mid_idx = start_idx + duration_weeks // 2
    chip_info = find_chip_dense_zone(df, start_idx + duration_weeks // 2)
    if chip_info:
        chip_zone = (chip_info[0], chip_info[1])
    
    return {
        'in_consolidation': True,
        'zone_low': float(zone_low),
        'zone_high': float(zone_high),
        'start_idx': start_idx,
        'end_idx': end_idx,
        'duration_weeks': duration_weeks,
        'amplitude_early': float(amp_early),
        'amplitude_late': float(amp_late),
        'reversal_count': reversal_count,
        'reversal_count_detail': reversal_count,
        'is_breakout': is_breakout,
        'breakout_volume_ratio': float(breakout_vol_ratio),
        'latest_close': float(latest_close),
        'chip_zone': chip_zone,
    }

def calc_20d_slope_bonus(df, idx, bullish=True, lookback=20):
    """计算20周斜率加分（复用原函数逻辑，改为周线）"""
    if idx < lookback:
        return 0
    seg = df.iloc[idx - lookback + 1:idx + 1]
    if len(seg) < lookback:
        return 0
    x = np.arange(len(seg))
    y = seg['Close'].astype(float).values
    if np.any(~np.isfinite(y)):
        return 0
    x_mean = x.mean()
    y_mean = y.mean()
    numerator = np.sum((x - x_mean) * (y - y_mean))
    denominator = np.sum((x - x_mean) ** 2)
    if denominator == 0:
        return 0
    slope = numerator / denominator
    current_price = float(df.iloc[idx]['Close'])
    if current_price == 0:
        return 0
    normalized_slope = slope / current_price * lookback
    
    if bullish:
        if normalized_slope > 0:
            bonus = min(SLOPE_BONUS_MAX, SLOPE_BONUS_MAX * min(normalized_slope / 0.05, 1.0))
            return round(bonus, 1)
    else:
        if normalized_slope < 0:
            bonus = min(SLOPE_BONUS_MAX, SLOPE_BONUS_MAX * min(abs(normalized_slope) / 0.05, 1.0))
            return round(bonus, 1)
    return 0

def scan_consolidation(symbol: str, df: pd.DataFrame):
    """对单个股票扫描底部盘整，返回结果字典或None"""
    info = detect_bottom_consolidation(df)
    if not info.get('in_consolidation', False):
        return None
    
    # 计算评分
    score = 50  # 基础分
    duration_weeks = info['duration_weeks']
    # 持续时间加分：每年 +10 分，上限约30年=300分（实际3年上限约30分）
    years = duration_weeks / 52.0
    score += min(30, int(years) * DURATION_BONUS_PER_YEAR)
    
    # 振幅收窄加分
    if info['amplitude_late'] < info['amplitude_early'] * AMPLITUDE_SHRINK_FACTOR:
        score += SHRINK_BONUS
    
    # 破底翻加分
    score += min(10, info['reversal_count'] * REVERSAL_BONUS_PER)
    
    # 底部位置：当前价格相对过去3年最高价的位置（越低越好）
    high3y = df['High'].max()
    if high3y > 0:
        price_position = info['latest_close'] / high3y
        if price_position < 0.6:
            score += 5
        elif price_position < 0.8:
            score += 2
    
    # 筹码密集区加分
    if info['chip_zone']:
        score += CHIP_BONUS
    
    # 20周斜率加分（做多方向）
    slope_bonus = calc_20d_slope_bonus(df, len(df)-1, bullish=True)
    score += slope_bonus
    
    # 突破额外加分
    is_breakout = info['is_breakout']
    if is_breakout and info['breakout_volume_ratio'] > BREAKOUT_VOL_MULTIPLIER:
        score += 5
    
    info = detect_bottom_consolidation(df)  # 重新获取以获取reversal_count_detail
    zone_low = info['zone_low']
    zone_high = info['zone_high']
    zone_text = f"{zone_low:.2f} - {zone_high:.2f}"
    if info['chip_zone']:
        zone_text += f" (筹码密集区 {info['chip_zone'][0]:.2f}-{info['chip_zone'][1]:.2f})"
    
    pattern = '底部突破' if info['is_breakout'] else '底部盘整'
    direction = '做多'
    event_date = df.iloc[info['start_idx']]['Date']
    confirm_date = df.iloc[-1]['Date']
    pullback_date = df.iloc[info['end_idx']]['Date']
    latest_close = info['latest_close']
    
    volume_feature = '放量突破' if info['is_breakout'] and info['breakout_volume_ratio'] > 1.2 else ('缩量' if info['breakout_volume_ratio'] < 0.8 else '量平')
    slowdown_feature = '振幅收窄' if info['amplitude_late'] < info['amplitude_early'] * AMPLITUDE_SHRINK_FACTOR else '振幅稳定'
    logic = f"盘整{info['duration_weeks']}周，破底翻{info['reversal_count']}次"
    if info['is_breakout']:
        logic += "，刚突破上沿"
    
    return {
        'symbol': symbol,
        'direction': '做多',
        'pattern': pattern,
        'zone': zone_text,
        'event_date': event_date.strftime('%Y-%m-%d'),
        'confirm_date': df.iloc[-1]['Date'].strftime('%Y-%m-%d'),
        'pullback_date': df.iloc[info['end_idx']]['Date'].strftime('%Y-%m-%d'),
        'price': round(info['latest_close'], 2),
        'fib618': round((info['zone_low'] + info['zone_high']) / 2, 2),
        'volume_feature': '放量突破' if info['is_breakout'] and info['breakout_volume_ratio'] > 1.2 else ('缩量' if info['breakout_volume_ratio'] < 0.8 else '量平'),
        'slowdown_feature': '振幅收窄' if info['amplitude_late'] < info['amplitude_early'] * AMPLITUDE_SHRINK_FACTOR else '振幅稳定',
        'score': round(score, 1),
        'logic': f"盘整{info['duration_weeks']}周，破底翻{info['reversal_count']}次" + ("，刚突破上沿" if info['is_breakout'] else ""),
        'recent_windows': [],
        '_sort_pullback': pd.Timestamp(df.iloc[info['end_idx']]['Date']),
        '_sort_event': pd.Timestamp(df.iloc[info['start_idx']]['Date']),
        '_sort_confirm': pd.Timestamp(df.iloc[-1]['Date']),
        '_is_breakout': info['is_breakout'],
        '_duration_weeks': info['duration_weeks'],
        'reversal_count': info['reversal_count'],
        'amplitude_early': round(info['amplitude_early'] * 100, 1),
        'amplitude_late': round(info['amplitude_late'] * 100, 1),
        'breakout_volume_ratio': round(info['breakout_volume_ratio'], 2),
    }

# ========== 缓存机制 ==========

class ConsolidationCache:
    """周线数据缓存，避免每周重复下载3年数据"""
    
    def __init__(self, cache_dir: Path, stderr_path: str = ''):
        self.cache_dir = cache_dir
        self.stderr_path = stderr_path
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.cache_dir / 'index.json'
        self.index = self._load_index()
    
    def _load_index(self):
        if self.index_file.exists():
            try:
                return json.loads(self.index_file.read_text())
            except:
                return {}
        return {}
    
    def _save_index(self):
        self.index_file.write_text(json.dumps(self.index, ensure_ascii=False, indent=2))
    
    def get_cached(self, symbol: str, max_age_days: int = CACHE_MAX_AGE_DAYS) -> pd.DataFrame | None:
        """获取缓存数据，若过期返回None"""
        cache_file = self.cache_dir / f"{symbol}.parquet"
        if not cache_file.exists():
            return None
        
        idx = self.index.get(symbol)
        if not idx:
            return None
        
        cached_time = datetime.fromisoformat(idx.get('timestamp', '2000-01-01'))
        if datetime.now(timezone.utc) - cached_time > timedelta(days=max_age_days):
            return None
        
        try:
            df = pd.read_parquet(cache_file)
            # 验证数据完整性
            if len(df) >= 52 and 'Close' in df.columns:
                return df
        except:
            pass
        return None
    
    def get_last_date(self, symbol: str) -> str | None:
        """返回缓存中该符号的最后日期（字符串），若无则返回None"""
        info = self.index.get(symbol)
        if info and 'last_date' in info:
            return info['last_date']
        return None
    
    def put(self, symbol: str, df: pd.DataFrame):
        """存入缓存，并记录最新日期"""
        cache_file = self.cache_dir / f"{symbol}.parquet"
        try:
            df.to_parquet(cache_file, compression='snappy')
            last_date = df['Date'].max().strftime('%Y-%m-%d')
            self.index[symbol] = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'rows': len(df),
                'date_range': f"{df['Date'].min()} ~ {df['Date'].max()}",
                'last_date': last_date,
            }
            self._save_index()
        except Exception as e:
            append_log(self.stderr_path, f"CACHE_PUT_ERROR {symbol} {e}")
    
    def merge_incremental(self, symbol: str, new_df: pd.DataFrame) -> pd.DataFrame:
        """增量合并：新数据与缓存合并，去重"""
        cached = self.get_cached(symbol, max_age_days=9999)  # 不检查过期，直接合并
        if cached is None or len(cached) == 0:
            return new_df
        
        # 合并并去重（按Date）
        merged = pd.concat([cached, new_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=['Date'], keep='last')
        merged = merged.sort_values('Date').reset_index(drop=True)
        
        # 只保留最近3年
        if len(merged) > MAX_CONSOLIDATION_WEEKS:
            merged = merged.tail(MAX_CONSOLIDATION_WEEKS).reset_index(drop=True)
        
        return merged

# ========== 修改后的扫描流程 ==========

def scan_stage2_dataset(stage2, mapped, stderr_path, cache):
    """扫描底部盘整，返回 (所有结果, 盘整中列表, 刚突破列表)"""
    results = []
    consolidating = []
    breaking_out = []
    
    for ys, df in stage2.items():
        try:
            df = df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume']).reset_index(drop=True)
            if len(df) < MIN_CONSOLIDATION_WEEKS:
                continue
            
            # 合并缓存
            df = cache.merge_incremental(ys, df)
            
            # 更新缓存
            cache.put(ys, df)
            
            res = scan_consolidation(mapped[ys], df)
            if res:
                res['symbol'] = mapped[ys]  # 还原原始symbol
                results.append(res)
                if res['_is_breakout']:
                    breaking_out.append(res)
                else:
                    consolidating.append(res)
        except Exception as e:
            append_log(stderr_path, f"SCAN_ERROR {ys} {e}\n{traceback.format_exc()}")
    
    return results, consolidating, breaking_out

# ========== 渲染报告 ==========

def render_markdown_report(out: dict) -> str:
    lines = []
    lines.append("📊 美股长期底部盘整扫描报告")
    lines.append("")

    generated_at = str(out.get('generated_at_utc', ''))
    report_date = generated_at[5:10] if len(generated_at) >= 10 else '未知'
    data_sources = out.get('data_sources') or ['Nasdaq Trader 月更股票池快取', 'Yahoo Finance / yfinance 周线 OHLCV']
    data_sources = [str(x).replace('日线', '周线') for x in data_sources]

    cons_list = out.get('top10_consolidating', [])
    break_list = out.get('top10_breaking_out', [])

    lines.append(f"🗂️ 数据来源：{'；'.join(data_sources)}")
    lines.append(f'📅 报告日期：{report_date}')
    lines.append("")

    miss_total = int(out.get('stage1_misses', 0)) + int(out.get('stage2_misses', 0))
    miss_note = f"；数据下载失败 {miss_total} 个" if miss_total else ""
    lines.append(
        f"摘要：共扫描 {out.get('universe_total', 0)} 个标的，"
        f"通过流动性过滤 {out.get('liquid_count', 0)} 个，"
        f"深度扫描 {out.get('deep_scan_count', 0)} 个，"
        f"底部盘整 {out.get('consolidating_count', 0)} 个，"
        f"刚突破 {out.get('breaking_out_count', 0)} 个，"
        f"最终输出前 {len(out.get('top10_consolidating', [])) + len(out.get('top10_breaking_out', []))} 个{miss_note}。"
    )
    lines.append("")

    def build_table(rows, title):
        if not rows:
            return f"**{title}**：无\n"
        table = f"**{title}**\n"
        table += "| 序 | 代码 | 区间 | 盘整周数 | 破底翻 | 现价 | 评分 | 逻辑 |\n"
        table += "|---|---|---|---|---|---:|---:|---|\n"
        for idx, row in enumerate(rows[:10], 1):
            symbol = row.get('symbol', '')
            zone = row.get('zone', '')
            weeks = row.get('_duration_weeks', 0)
            rev = row.get('reversal_count', 0)
            price = row.get('price', 0)
            scr = row.get('score', 0)
            logic = row.get('logic', '')
            table += f"| {idx} | {symbol} | {zone} | {weeks} | {rev} | {price:.2f} | {scr:.1f} | {logic} |\n"
        return table + "\n"

    lines.append('## 🟡 盘整中（前10）')
    lines.append(build_table(cons_list, '底部盘整中'))
    lines.append('## 🟢 刚突破（前10）')
    lines.append(build_table(break_list, '底部突破'))

    lines.append('⚠️ 风险提示：此为AI扫描结果，仅供参考，不构成投资建议。')
    return '\n'.join(lines)

# ========== Discord 渲染 ==========

def render_discord(out: dict) -> dict:
    """生成 Discord Embed payload"""
    generated_at = out.get('generated_at_utc', datetime.now(timezone.utc).isoformat())
    scan_date = generated_at[:10]
    cons = out.get('top10_consolidating', [])
    break_out = out.get('top10_breaking_out', [])
    universe = out.get('universe_total', 'N/A')
    liquid = out.get('liquid_count', 'N/A')
    deep = out.get('deep_scan_count', 'N/A')
    
    def build_field_value(rows, title):
        if not rows:
            return "```text\n  --  无  --\n```"
        lines = ["```text"]
        lines.append(f" {title} (Top {len(rows[:10])})")
        lines.append(" 序  代码    盘整周  破底翻  评分")
        for idx, row in enumerate(rows[:10], 1):
            symbol = row.get('symbol', '')
            weeks = row.get('_duration_weeks', 0)
            rev = row.get('reversal_count', 0)
            scr = row.get('score', 0)
            lines.append(f"{idx:2d}. {symbol:<6} {weeks:3d}周   {rev:2d}次   {scr:5.1f}")
        lines.append("```")
        return '\n'.join(lines)
    
    embed = {
        "title": "📊 长期底部盘整扫描",
        "description": f"**日期**: {scan_date}\n📊 扫描 {universe} 支 → 流动性 {liquid} 支 → 深度扫 {deep} 支\n🟡 盘整中 {len(cons)} ｜ 🟢 刚突破 {len(break_out)}",
        "color": 0x2B2D42,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "周线数据 3年 | 仅供研究参考"},
        "fields": [
            {"name": f"🟡 盘整中（前10）", "value": build_field_value(cons, "盘整中"), "inline": False},
            {"name": f"🟢 刚突破（前10）", "value": build_field_value(break_out, "刚突破"), "inline": False},
        ]
    }
    return {"embeds": [embed], "username": "Consolidation Scanner"}

# ========== 主流程 ==========

stderr_path = ""

def main():
    global stderr_path
    
    parser = argparse.ArgumentParser(description='U.S. Consolidation Base Scanner (3y weekly)')
    parser.add_argument('--format', choices=['json', 'markdown'], default='json')
    parser.add_argument('--max-symbols', type=int, default=0, help='Optional cap on universe size for smoke tests')
    parser.add_argument('--stderr-path', default='/tmp/consolidation_scan_yf_stderr.log')
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

    # 缓存目录
    cache_dir = artifact_dir / '.consolidation_cache'
    cache = ConsolidationCache(cache_dir, stderr_path)

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
    consolidating = []
    breaking_out = []
    deep_scan_count = 0
    miss2 = set()
    shard_summaries = []

    for shard_idx, shard_symbols in enumerate(shard_lists, start=1):
        append_log(stderr_path, f"STAGE2_SHARD_START shard={shard_idx}/{len(shard_lists)} symbols={len(shard_symbols)}")
        # 修改：周线 3年
        stage2, shard_miss = download_bars(shard_symbols, '3y', stderr_path, batch=args.stage2_batch, interval='1wk', phase=f'STAGE2_SHARD_{shard_idx:02d}')
        shard_results, cons_list, break_list = scan_stage2_dataset(stage2, mapped, stderr_path, cache)
        deep_scan_count += len(stage2)
        miss2.update(shard_miss)
        results.extend(shard_results)
        consolidating.extend(cons_list)
        breaking_out.extend(break_list)
        
        shard_summary = {
            'shard': shard_idx,
            'input_symbols': len(shard_symbols),
            'downloaded_symbols': len(stage2),
            'misses': len(shard_miss),
            'candidates': len(shard_results),
            'consolidating': len(cons_list),
            'breaking_out': len(break_list),
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

    # 按分数排序
    consolidating.sort(key=lambda x: (x['score'], x['_duration_weeks']), reverse=True)
    breaking_out.sort(key=lambda x: (x['score'], x['_duration_weeks']), reverse=True)
    
    top10_cons = consolidating[:10]
    top10_break = breaking_out[:10]
    
    # 输出
    out = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'data_sources': [
            'Nasdaq Trader nasdaqlisted.txt',
            'Nasdaq Trader otherlisted.txt',
            'Yahoo Finance / yfinance 周线 OHLCV (3年)',
        ],
        'universe_total': int(len(original_symbols)),
        'liquid_count': int(len(liquid)),
        'deep_scan_count': int(deep_scan_count),
        'stage1_misses': int(len(miss1)),
        'stage2_misses': int(len(miss2)),
        'candidate_total': int(len(results)),
        'consolidating_count': int(len(consolidating)),
        'breaking_out_count': int(len(breaking_out)),
        'stderr_log': stderr_path,
        'artifact_dir': str(artifact_dir),
        'shard_count': len(shard_lists),
        'shards': shard_summaries,
        'top10_consolidating': top10_cons,
        'top10_breaking_out': top10_break,
    }
    (artifact_dir / 'final_output.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    append_log(stderr_path, f"SCAN_DONE deep_scan={deep_scan_count} candidates={len(results)} consolidating={len(consolidating)} breaking_out={len(breaking_out)}")
    
    if args.format == 'markdown':
        print(render_markdown_report(out))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))

if __name__ == '__main__':
    main()