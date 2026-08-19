#!/usr/bin/env python3
import argparse
import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from us_pattern_scan import (
    NASDAQ_LISTED_URL,
    OTHER_LISTED_URL,
    append_log,
    download_bars,
    fetch_text,
    is_regular_security,
    local_extrema,
    parse_nasdaq_listed,
    parse_other_listed,
    split_into_shards,
    trailing_avg_dollar_volume,
    yahoo_symbol,
)

def pct(a, b):
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0

def rolling_sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()

# ============================================================
# 參數配置加載
# ============================================================
CONFIG_PATH = Path(__file__).parent / "config" / "vcp_params.yaml"

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Warning: Failed to load config: {e}, using defaults")
        return {}

CONFIG = load_config()

# ============================================================
# 動態參數（環境變量優先）
# ============================================================
def get_env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))

def get_env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))

_vcp = CONFIG.get("vcp", {})
_near_pivot = CONFIG.get("near_pivot", {})
_volume = CONFIG.get("volume", {})
_structure = CONFIG.get("structure", {})
_fibonacci = CONFIG.get("fibonacci", {})
_confirmation = CONFIG.get("confirmation", {})
_scoring = CONFIG.get("scoring", {})
_dynamic = CONFIG.get("dynamic_adjustment", {})
_data_fetch = CONFIG.get("data_fetch", {})
_parallel = CONFIG.get("parallel", {})
_output = CONFIG.get("output", {})
_quality_filter = CONFIG.get("quality_filter", {})
_cup = CONFIG.get("cup_handle", {})
_risk = CONFIG.get("risk_disclaimer", "這是 AI 掃描出的參考買賣點，不涉及投資建議，做多/做空有風險。")

# VCP 核心參數（收紧版）
PRICE_HIGH_LOOKBACK = get_env_int("HERMES_VCP_PRICE_HIGH_LOOKBACK", _vcp.get("price_high_lookback", 120))
CONTRACTION_LOOKBACK = get_env_int("HERMES_VCP_CONTRACTION_LOOKBACK", _vcp.get("contraction_lookback", 80))
PIVOT_WINDOW = get_env_int("HERMES_VCP_PIVOT_WINDOW", _vcp.get("pivot_window", 15))
LAST_TIGHT_WINDOW = get_env_int("HERMES_VCP_LAST_TIGHT_WINDOW", _vcp.get("last_tight_window", 10))
CUP_LOOKBACK = get_env_int("HERMES_VCP_CUP_LOOKBACK", _vcp.get("cup_lookback", 180))
CUP_MIN_BARS = get_env_int("HERMES_VCP_CUP_MIN_BARS", _vcp.get("cup_min_bars", 30))
HANDLE_MIN_BARS = get_env_int("HERMES_VCP_HANDLE_MIN_BARS", _vcp.get("handle_min_bars", 3))
HANDLE_MAX_BARS = get_env_int("HERMES_VCP_HANDLE_MAX_BARS", _vcp.get("handle_max_bars", 25))

MIN_DRAWDOWN = get_env_float("HERMES_VCP_MIN_DRAWDOWN", _vcp.get("min_drawdown", 0.03))
MAX_DRAWDOWN = get_env_float("HERMES_VCP_MAX_DRAWDOWN", _vcp.get("max_drawdown", 0.35))
DRAWDOWN_DECREASE_FACTOR = get_env_float("HERMES_VCP_DRAWDOWN_DECREASE_FACTOR", _vcp.get("drawdown_decrease_factor", 0.80))
DRAWDOWN_DECREASE_OFFSET = get_env_float("HERMES_VCP_DRAWDOWN_DECREASE_OFFSET", _vcp.get("drawdown_decrease_offset", 0.005))
LOW_PRICE_TOLERANCE = get_env_float("HERMES_VCP_LOW_PRICE_TOLERANCE", _vcp.get("low_price_tolerance", 0.95))
MIN_BARS_BETWEEN_LOWS = get_env_int("HERMES_VCP_MIN_BARS_BETWEEN_LOWS", _vcp.get("min_bars_between_lows", 5))
MIN_PULLBACKS = get_env_int("HERMES_VCP_MIN_PULLBACKS", _vcp.get("min_pullbacks", 3))
MAX_PULLBACKS = get_env_int("HERMES_VCP_MAX_PULLBACKS", _vcp.get("max_pullbacks", 4))
LAST_LOW_RECENCY = get_env_int("HERMES_VCP_LAST_LOW_RECENCY", _vcp.get("last_low_recency", 15))
TIGHT_RANGE_MAX_PCT = get_env_float("HERMES_VCP_TIGHT_RANGE_MAX_PCT", _vcp.get("tight_range_max_pct", 7.0))

NEAR_PIVOT_MIN_PCT = get_env_float("HERMES_VCP_NEAR_PIVOT_MIN_PCT", _near_pivot.get("window_min_pct", -6.0))
NEAR_PIVOT_MAX_PCT = get_env_float("HERMES_VCP_NEAR_PIVOT_MAX_PCT", _near_pivot.get("window_max_pct", 3.0))

VOLUME_DRY_THRESHOLD = get_env_float("HERMES_VCP_VOLUME_DRY_THRESHOLD", _volume.get("dry_threshold", 0.85))
BREAKOUT_VOL_MULTIPLIER = get_env_float("HERMES_VCP_BREAKOUT_VOL_MULTIPLIER", _volume.get("breakout_vol_multiplier", 1.2))
VOLUME_SHRINK_THRESHOLD = get_env_float("HERMES_VCP_VOLUME_SHRINK_THRESHOLD", _volume.get("shrink_threshold", 0.85))

SHORT_TREND_LOOKBACK = get_env_int("HERMES_VCP_SHORT_TREND_LOOKBACK", _structure.get("short_trend_lookback", 30))
LONG_TREND_LOOKBACK = get_env_int("HERMES_VCP_LONG_TREND_LOOKBACK", _structure.get("long_trend_lookback", 90))
SWING_WINDOW = get_env_int("HERMES_VCP_SWING_WINDOW", _structure.get("swing_window", 3))
MIN_BARS_BETWEEN_SWINGS = get_env_int("HERMES_VCP_MIN_BARS_BETWEEN_SWINGS", _structure.get("min_bars_between_swings", 5))
LONG_TERM_TREND_BONUS = get_env_float("HERMES_VCP_LONG_TERM_TREND_BONUS", _structure.get("long_term_trend_bonus", 5.0))

FIB_MAX_RECLAIM_DAYS = get_env_int("HERMES_VCP_FIB_MAX_RECLAIM_DAYS", _fibonacci.get("max_reclaim_days", 5))
FIB_RECLAIM_CLOSE_ABOVE = get_env_float("HERMES_VCP_FIB_RECLAIM_CLOSE_ABOVE", _fibonacci.get("reclaim_close_above", True))
FIB_RECLAIM_GAP_OK = get_env_float("HERMES_VCP_FIB_RECLAIM_GAP_OK", _fibonacci.get("reclaim_gap_ok", True))
FIB_TOUCH_TOLERANCE = get_env_float("HERMES_VCP_FIB_TOUCH_TOLERANCE", _fibonacci.get("touch_tolerance_pct", 0.015))

CONF_MIN_BODY_SHRINK = get_env_float("HERMES_VCP_CONF_MIN_BODY_SHRINK", _confirmation.get("min_body_shrink_pct", 0.85))
CONF_MIN_TR_SHRINK = get_env_float("HERMES_VCP_CONF_MIN_TR_SHRINK", _confirmation.get("min_tr_shrink_pct", 0.90))
CONF_VOL_SHRINK = get_env_float("HERMES_VCP_CONF_VOL_SHRINK", _confirmation.get("vol_shrink_threshold", 1.0))

VCP_BASE_SCORE = get_env_float("HERMES_VCP_BASE_SCORE", _scoring.get("vcp", {}).get("base_score", 50.0))
VCP_PER_CONTRACTION = get_env_float("HERMES_VCP_PER_CONTRACTION", _scoring.get("vcp", {}).get("per_contraction", 8.0))
VCP_DISTANCE_WEIGHT = get_env_float("HERMES_VCP_DISTANCE_WEIGHT", _scoring.get("vcp", {}).get("distance_weight", 2.0))
VCP_TIGHT_RANGE_WEIGHT = get_env_float("HERMES_VCP_TIGHT_RANGE_WEIGHT", _scoring.get("vcp", {}).get("tight_range_weight", 0.8))
VCP_TREND_STRENGTH_WEIGHT = get_env_float("HERMES_VCP_TREND_STRENGTH_WEIGHT", _scoring.get("vcp", {}).get("trend_strength_weight", 0.6))
VCP_VOLUME_DRY_BONUS = get_env_float("HERMES_VCP_VOLUME_DRY_BONUS", _scoring.get("vcp", {}).get("volume_dry_bonus", 8.0))
VCP_BREAKOUT_VOL_BONUS = get_env_float("HERMES_VCP_BREAKOUT_VOL_BONUS", _scoring.get("vcp", {}).get("breakout_vol_bonus", 8.0))
VCP_BREAKOUT_TODAY_BONUS = get_env_float("HERMES_VCP_BREAKOUT_TODAY_BONUS", _scoring.get("vcp", {}).get("breakout_today_bonus", 4.0))
VCP_STRONG_CONTRACTION_BONUS = get_env_float("HERMES_VCP_STRONG_CONTRACTION_BONUS", _scoring.get("vcp", {}).get("strong_contraction_bonus", 6.0))

CUP_BASE_SCORE = get_env_float("HERMES_CUP_BASE_SCORE", _scoring.get("cup_handle", {}).get("base_score", 56.0))
CUP_DISTANCE_WEIGHT = get_env_float("HERMES_CUP_DISTANCE_WEIGHT", _scoring.get("cup_handle", {}).get("distance_weight", 2.0))
CUP_DEPTH_WEIGHT = get_env_float("HERMES_CUP_DEPTH_WEIGHT", _scoring.get("cup_handle", {}).get("cup_depth_weight", 30.0))
CUP_HANDLE_DEPTH_WEIGHT = get_env_float("HERMES_CUP_HANDLE_DEPTH_WEIGHT", _scoring.get("cup_handle", {}).get("handle_depth_weight", 80.0))
CUP_TREND_STRENGTH_WEIGHT = get_env_float("HERMES_CUP_TREND_STRENGTH_WEIGHT", _scoring.get("cup_handle", {}).get("trend_strength_weight", 0.6))
CUP_VOLUME_DRY_BONUS = get_env_float("HERMES_CUP_VOLUME_DRY_BONUS", _scoring.get("cup_handle", {}).get("volume_dry_bonus", 6.0))
CUP_BREAKOUT_VOL_BONUS = get_env_float("HERMES_CUP_BREAKOUT_VOL_BONUS", _scoring.get("cup_handle", {}).get("breakout_vol_bonus", 8.0))
CUP_BREAKOUT_TODAY_BONUS = get_env_float("HERMES_CUP_BREAKOUT_TODAY_BONUS", _scoring.get("cup_handle", {}).get("breakout_today_bonus", 4.0))
CUP_HANDLE_DAYS_BONUS = get_env_float("HERMES_CUP_HANDLE_DAYS_BONUS", _scoring.get("cup_handle", {}).get("handle_days_bonus", 3.0))

DYNAMIC_ADJUSTMENT_ENABLED = _dynamic.get("enabled", True)
ATR_PERIOD = _dynamic.get("atr_period", 14)
HIGH_VOL_PERCENTILE = _dynamic.get("high_volatility_percentile", 80)
LOW_VOL_PERCENTILE = _dynamic.get("low_volatility_percentile", 20)

MIN_PRICE_TO_YEAR_HIGH_PCT = CONFIG.get("trend_filter", {}).get("min_price_to_year_high_pct", 0.65)
HANDLE_BELOW_MIDLINE_PENALTY = CONFIG.get("cup_handle", {}).get("handle_below_midline_penalty", -3.0)

# 杯柄严格参数
RIM_ALIGNMENT_MIN = get_env_float("HERMES_VCP_RIM_ALIGNMENT_MIN", _cup.get("rim_alignment_min", 0.85))
RIM_ALIGNMENT_MAX = get_env_float("HERMES_VCP_RIM_ALIGNMENT_MAX", _cup.get("rim_alignment_max", 1.08))
CUP_DEPTH_MIN = get_env_float("HERMES_VCP_CUP_DEPTH_MIN", _cup.get("cup_depth_min", 0.10))
CUP_DEPTH_MAX = get_env_float("HERMES_VCP_CUP_DEPTH_MAX", _cup.get("cup_depth_max", 0.45))
HANDLE_DEPTH_MIN = get_env_float("HERMES_VCP_HANDLE_DEPTH_MIN", _cup.get("handle_depth_min", 0.02))
HANDLE_DEPTH_MAX = get_env_float("HERMES_VCP_HANDLE_DEPTH_MAX", _cup.get("handle_depth_max", 0.20))
HANDLE_MAX_HEIGHT_PCT = get_env_float("HERMES_VCP_HANDLE_MAX_HEIGHT_PCT", _cup.get("handle_max_height_pct", 0.10))
HANDLE_ALLOW_BELOW_MIDLINE = get_env_float("HERMES_VCP_HANDLE_ALLOW_BELOW_MIDLINE", _cup.get("handle_allow_below_midline", False))

STAGE1_PERIOD = os.environ.get('HERMES_VCP_STAGE1_PERIOD', _data_fetch.get("stage1_period", "20d"))
STAGE2_PERIOD = os.environ.get('HERMES_VCP_STAGE2_PERIOD', _data_fetch.get("stage2_period", "10mo"))
STAGE1_BATCH = get_env_int("HERMES_VCP_STAGE1_BATCH", _data_fetch.get("stage1_batch", 90))
STAGE2_BATCH = get_env_int("HERMES_VCP_STAGE2_BATCH", _data_fetch.get("stage2_batch", 100))
DOWNLOAD_TIMEOUT = get_env_int("HERMES_VCP_DOWNLOAD_TIMEOUT", _data_fetch.get("download_timeout", 30))
MAX_RETRIES = get_env_int("HERMES_VCP_MAX_RETRIES", _data_fetch.get("max_retries", 3))
RETRY_DELAY = get_env_float("HERMES_VCP_RETRY_DELAY", _data_fetch.get("retry_delay", 5.0))
NASDAQ_CACHE_HOURS = get_env_int("HERMES_VCP_NASDAQ_CACHE_HOURS", _data_fetch.get("nasdaq_cache_hours", 24))

STAGE1_BATCH_SIZE = get_env_int("HERMES_VCP_STAGE1_BATCH", _parallel.get("stage1_batch_size", 90))
STAGE2_BATCH_SIZE = get_env_int("HERMES_VCP_STAGE2_BATCH", _parallel.get("stage2_batch_size", 100))
MAX_WORKERS = get_env_int("HERMES_VCP_MAX_WORKERS", _parallel.get("max_workers", 8))
UNIVERSE_SHARD_COUNT = get_env_int("HERMES_VCP_UNIVERSE_SHARD_COUNT", _parallel.get("universe_shard_count", 18))
WORKER_CONCURRENCY = get_env_int("HERMES_VCP_WORKER_CONCURRENCY", _parallel.get("worker_concurrency", 6))

TOP_N = get_env_int("HERMES_VCP_TOP_N", _output.get("top_n", 10))
MARKDOWN_FORMAT = _output.get("markdown_format", True)
INCLUDE_LOGIC = _output.get("include_logic", True)
INCLUDE_RECENT_WINDOWS = _output.get("include_recent_windows", True)

QUALITY_FILTER_ENABLED = _quality_filter.get("enabled", True)
QUALITY_MIN_SCORE_BY_STAGE = _quality_filter.get("min_score_by_stage", {
    "剛突破": 65.0,
    "近突破點": 55.0,
    "整理末端": 40.0,
})
DEFAULT_QUALITY_MIN_SCORE = _quality_filter.get("default_min_score", 50.0)

RISK_DISCLAIMER = _risk

# ============================================================
# 核心函数
# ============================================================

def calculate_atr(df, period=14):
    high = df['High'].astype(float)
    low = df['Low'].astype(float)
    close = df['Close'].astype(float)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def detect_vcp(symbol: str, df: pd.DataFrame, stderr_path: str = None):
    df = df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume']).reset_index(drop=True).copy()
    if len(df) < 180:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: insufficient data ({len(df)} < 180)")
        return None

    close = df['Close'].astype(float)
    high = df['High'].astype(float)
    low = df['Low'].astype(float)
    vol = df['Volume'].astype(float)
    df['sma50'] = rolling_sma(close, 50)
    df['sma150'] = rolling_sma(close, 150)
    if pd.isna(df.iloc[-1]['sma50']) or pd.isna(df.iloc[-1]['sma150']) or pd.isna(df.iloc[-21]['sma150']):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: SMA NaN (sma50={df.iloc[-1]['sma50']}, sma150={df.iloc[-1]['sma150']}, sma150_prev20={df.iloc[-21]['sma150']})")
        return None

    latest_close = float(close.iloc[-1])
    sma50 = float(df.iloc[-1]['sma50'])
    sma150 = float(df.iloc[-1]['sma150'])
    sma150_prev20 = float(df.iloc[-21]['sma150'])
    year_high = float(high.tail(PRICE_HIGH_LOOKBACK).max())
    if year_high <= 0:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: year_high <= 0")
        return None

    # 趨勢結構：不再硬性要求，改為加分項
    trend_structure_ok = (latest_close > sma150 and sma150 > sma150_prev20)
    sma50_bonus = 0.0
    if latest_close > sma50 > sma150:
        sma50_bonus = 3.0

    # 價格相對 52 週高點：min_price_to_year_high_pct=0 時不限制
    min_price_to_year_high = MIN_PRICE_TO_YEAR_HIGH_PCT
    if DYNAMIC_ADJUSTMENT_ENABLED and len(df) >= ATR_PERIOD + 20:
        atr_series = calculate_atr(df, ATR_PERIOD)
        if not pd.isna(atr_series.iloc[-1]):
            atr_pct = (atr_series.iloc[-1] / latest_close) * 100
            if atr_pct > 3.0:
                min_price_to_year_high = max(0.55, MIN_PRICE_TO_YEAR_HIGH_PCT - 0.05)
            elif atr_pct < 1.5:
                min_price_to_year_high = min(0.75, MIN_PRICE_TO_YEAR_HIGH_PCT + 0.05)

    min_price_threshold = year_high * min_price_to_year_high
    if min_price_to_year_high > 0 and latest_close < min_price_threshold:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: price below {min_price_to_year_high:.0%} of year_high (close={latest_close:.2f}, year_high={year_high:.2f}, threshold={min_price_threshold:.2f})")
        return None

    avg_dollar_vol_20 = float((close.tail(20) * vol.tail(20)).mean())
    if not np.isfinite(avg_dollar_vol_20) or avg_dollar_vol_20 < 20_000_000:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: avg dollar vol < 20M ({avg_dollar_vol_20:,.0f})")
        return None

    recent = df.tail(CONTRACTION_LOOKBACK).reset_index(drop=True)
    _, highs = local_extrema(recent, 'high', lookback=len(recent), window=3, max_results=None)
    _, lows = local_extrema(recent, 'low', lookback=len(recent), window=3, max_results=None)
    if len(highs) < 2 or len(lows) < 2:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: insufficient extrema (highs={len(highs)}, lows={len(lows)})")
        return None

    pullbacks = []
    for li in lows:
        prev_highs = [h for h in highs if h < li]
        if not prev_highs:
            continue
        hi = prev_highs[-1]
        if li - hi < MIN_BARS_BETWEEN_LOWS:
            continue
        h_price = float(recent.iloc[hi]['High'])
        l_price = float(recent.iloc[li]['Low'])
        if h_price <= 0 or l_price <= 0 or l_price >= h_price:
            continue
        dd = (h_price - l_price) / h_price
        if not (MIN_DRAWDOWN <= dd <= MAX_DRAWDOWN):
            if stderr_path:
                append_log(stderr_path, f"DEBUG_VCP {symbol}: pullback dd={dd:.2%} out of range [{MIN_DRAWDOWN:.0%},{MAX_DRAWDOWN:.0%}] (high_idx={hi}, low_idx={li})")
            continue
        pullbacks.append({
            'high_idx': hi,
            'low_idx': li,
            'high_price': h_price,
            'low_price': l_price,
            'drawdown': dd,
            'date': recent.iloc[li]['Date'],
        })

    if len(pullbacks) < MIN_PULLBACKS:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: pullbacks={len(pullbacks)} < MIN_PULLBACKS={MIN_PULLBACKS}")
        return None

    best = None
    max_len = min(MAX_PULLBACKS, len(pullbacks))
    for seq_len in range(MIN_PULLBACKS, max_len + 1):
        for start in range(0, len(pullbacks) - seq_len + 1):
            seq = pullbacks[start:start + seq_len]
            ok = True
            for i in range(1, len(seq)):
                prev = seq[i - 1]
                cur = seq[i]
                # 放寬：只要後波回撤 < 前波即可（factor=1.0, offset=0）
                if cur['drawdown'] >= prev['drawdown']:
                    if stderr_path:
                        append_log(stderr_path, f"DEBUG_VCP {symbol}: contraction fail dd_not_decrease (prev_dd={prev['drawdown']:.2%}, cur_dd={cur['drawdown']:.2%})")
                    ok = False
                    break
                # 低點容忍：low_price_tolerance=1.0 允許平齊或略微下探
                if cur['low_price'] < prev['low_price'] * LOW_PRICE_TOLERANCE:
                    if stderr_path:
                        append_log(stderr_path, f"DEBUG_VCP {symbol}: contraction fail low_not_higher (prev_low={prev['low_price']:.2f}, cur_low={cur['low_price']:.2f}, tolerance={LOW_PRICE_TOLERANCE})")
                    ok = False
                    break
                if cur['low_idx'] - prev['low_idx'] < MIN_BARS_BETWEEN_LOWS:
                    if stderr_path:
                        append_log(stderr_path, f"DEBUG_VCP {symbol}: contraction fail bars_between_lows (prev_idx={prev['low_idx']}, cur_idx={cur['low_idx']}, min={MIN_BARS_BETWEEN_LOWS})")
                    ok = False
                    break
            if not ok:
                continue
            last_low_idx = seq[-1]['low_idx']
            if last_low_idx < len(recent) - LAST_LOW_RECENCY:
                if stderr_path:
                    append_log(stderr_path, f"DEBUG_VCP {symbol}: last_low too old (idx={last_low_idx}, recency_limit={LAST_LOW_RECENCY}, recent_len={len(recent)})")
                continue
            if best is None or (len(seq), seq[-1]['low_idx'], -seq[-1]['drawdown']) > (len(best), best[-1]['low_idx'], -best[-1]['drawdown']):
                best = seq

    if not best:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: no valid contraction sequence found")
        return None

    # 高点收敛检查 - 改為加分項
    high_prices = [float(recent.iloc[p['high_idx']]['High']) for p in best]
    highs_converging = True
    for i in range(1, len(high_prices)):
        if high_prices[i] >= high_prices[i-1]:
            highs_converging = False
            if stderr_path:
                append_log(stderr_path, f"DEBUG_VCP {symbol}: highs not converging (high_prices={high_prices}) - no bonus")
            break

    # 波动率收缩检查 - 改為加分項
    atr_80 = calculate_atr(df, 80).iloc[-1]
    atr_20 = calculate_atr(df, 20).iloc[-1]
    atr_contracted = False
    if not pd.isna(atr_80) and not pd.isna(atr_20) and atr_20 <= atr_80 * 0.85:
        atr_contracted = True
    else:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: ATR not contracted (atr_20={atr_20:.4f}, atr_80={atr_80:.4f}, ratio={atr_20/atr_80:.2f}) - no bonus")

    latest_seg = df.tail(LAST_TIGHT_WINDOW)
    tight_range_pct = (float(latest_seg['High'].max()) - float(latest_seg['Low'].min())) / latest_close * 100.0
    if tight_range_pct > TIGHT_RANGE_MAX_PCT:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: tight_range_pct={tight_range_pct:.2f}% > max={TIGHT_RANGE_MAX_PCT}%")
        return None

    pivot_seg = df.iloc[-(PIVOT_WINDOW + 1):-1].copy()
    if len(pivot_seg) < PIVOT_WINDOW:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: pivot_seg too short ({len(pivot_seg)} < {PIVOT_WINDOW})")
        return None
    pivot = float(pivot_seg['High'].max())
    if pivot <= 0:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: pivot <= 0")
        return None
    distance_to_pivot_pct = pct(latest_close, pivot)
    if distance_to_pivot_pct < NEAR_PIVOT_MIN_PCT or distance_to_pivot_pct > NEAR_PIVOT_MAX_PCT:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: distance_to_pivot={distance_to_pivot_pct:.2f}% out of range [{NEAR_PIVOT_MIN_PCT},{NEAR_PIVOT_MAX_PCT}] (pivot={pivot:.2f})")
        return None

    avg_vol_10 = float(vol.tail(10).mean())
    avg_vol_20 = float(vol.tail(20).mean())
    avg_vol_50 = float(vol.tail(50).mean())
    volume_dry = avg_vol_10 < avg_vol_50 * VOLUME_DRY_THRESHOLD
    breakout_today = latest_close > pivot
    breakout_vol = float(vol.iloc[-1]) > avg_vol_20 * BREAKOUT_VOL_MULTIPLIER if avg_vol_20 > 0 else False
    near_pivot = NEAR_PIVOT_MIN_PCT <= distance_to_pivot_pct <= NEAR_PIVOT_MAX_PCT

    # 成交量/位置條件：只要滿足其一即可（不再硬性要求 volume_dry 或 breakout_vol）
    if not (breakout_today or volume_dry or near_pivot):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: volume/position condition fail (breakout_today={breakout_today}, volume_dry={volume_dry}, near_pivot={near_pivot}, vol10={avg_vol_10:.0f}, vol50={avg_vol_50:.0f}, threshold={VOLUME_DRY_THRESHOLD})")
        return None

    # 突破日成交量：不再硬性要求 1.5 倍
    if breakout_today:
        vol_ratio = float(vol.iloc[-1]) / avg_vol_20 if avg_vol_20 > 0 else 0
        # 不再因 vol_ratio < 1.5 而拒絕

    breakout_date = None
    if breakout_today:
        breakout_idx = len(df) - 1
        while breakout_idx > 0 and float(df.iloc[breakout_idx - 1]['Close']) > pivot:
            breakout_idx -= 1
        breakout_date = pd.Timestamp(df.iloc[breakout_idx]['Date'])

    contraction_pcts = [round(x['drawdown'] * 100.0, 1) for x in best]
    if contraction_pcts[-1] > contraction_pcts[0]:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: last contraction > first ({contraction_pcts})")
        return None

    last_low = best[-1]
    last_low_global = len(df) - len(recent) + last_low['low_idx']
    if last_low_global < 20:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: last_low_global < 20 ({last_low_global})")
        return None
    last_low_avg_dv = trailing_avg_dollar_volume(df, last_low_global, days=20)
    if last_low_avg_dv is None or last_low_avg_dv < 20_000_000:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_VCP {symbol}: last_low avg dollar vol < 20M ({last_low_avg_dv})")
        return None

    stage = '剛突破' if breakout_today else ('近突破點' if near_pivot else '整理末端')

    if stage == '剛突破' and breakout_date is not None:
        days_since_breakout = (pd.Timestamp(df.iloc[-1]['Date']) - breakout_date).days
        if days_since_breakout > 5:
            if stderr_path:
                append_log(stderr_path, f"DEBUG_VCP {symbol}: days_since_breakout={days_since_breakout} > 5")
            return None

    score = VCP_BASE_SCORE
    score += VCP_PER_CONTRACTION * len(best)
    score += max(0.0, 20.0 - abs(distance_to_pivot_pct) * VCP_DISTANCE_WEIGHT)
    score += max(0.0, 10.0 - tight_range_pct * VCP_TIGHT_RANGE_WEIGHT)
    score += min(10.0, pct(latest_close, sma50) * VCP_TREND_STRENGTH_WEIGHT)
    score += sma50_bonus
    # 新增加分項（配合 config.yaml）
    if trend_structure_ok:
        score += 10.0  # trend_structure_bonus
    if atr_contracted:
        score += 8.0   # atr_contraction_bonus
    if highs_converging:
        score += 8.0   # highs_convergence_bonus
    if volume_dry:
        score += VCP_VOLUME_DRY_BONUS
    if breakout_today and breakout_vol:
        score += VCP_BREAKOUT_VOL_BONUS
    elif breakout_today:
        score += VCP_BREAKOUT_TODAY_BONUS
    if contraction_pcts[-1] <= contraction_pcts[0] * 0.60:
        score += VCP_STRONG_CONTRACTION_BONUS

    if stderr_path:
        append_log(stderr_path, f"DEBUG_VCP {symbol}: PASS stage={stage} score={score:.1f} contractions={len(best)} pivot={pivot:.2f} dist={distance_to_pivot_pct:.2f}% tight={tight_range_pct:.2f}% vol_dry={volume_dry}")

    return {
        'symbol': symbol,
        'stage': stage,
        'pattern': 'VCP',
        'feature_label': f"{len(contraction_pcts)}次收縮",
        'contractions': contraction_pcts,
        'contraction_count': len(contraction_pcts),
        'pivot': round(pivot, 2),
        'price': round(latest_close, 2),
        'distance_to_pivot_pct': round(distance_to_pivot_pct, 2),
        'tight_range_pct_10d': round(tight_range_pct, 2),
        'volume_feature': '量縮乾淨' if volume_dry else ('突破放量' if breakout_today and breakout_vol else '一般'),
        'last_pullback_date': pd.Timestamp(last_low['date']).strftime('%Y-%m-%d'),
        'breakout_date': breakout_date.strftime('%Y-%m-%d') if breakout_date is not None else '',
        'avg_20d_dollar_volume_m': round(avg_dollar_vol_20 / 1_000_000, 1),
        'score': round(score, 1),
        '_sort_score': score,
        '_sort_breakout_date': breakout_date.isoformat() if breakout_date is not None else '',
        '_sort_pullback_date': pd.Timestamp(last_low['date']).isoformat(),
    }

def detect_cup_handle(symbol: str, df: pd.DataFrame, stderr_path: str = None):
    df = df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume']).reset_index(drop=True).copy()
    if len(df) < 200:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: insufficient data ({len(df)} < 200)")
        return None

    close = df['Close'].astype(float)
    high = df['High'].astype(float)
    low = df['Low'].astype(float)
    vol = df['Volume'].astype(float)
    df['sma50'] = rolling_sma(close, 50)
    df['sma150'] = rolling_sma(close, 150)
    if pd.isna(df.iloc[-1]['sma50']) or pd.isna(df.iloc[-1]['sma150']) or pd.isna(df.iloc[-21]['sma150']):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: SMA NaN (sma50={df.iloc[-1]['sma50']}, sma150={df.iloc[-1]['sma150']}, sma150_prev20={df.iloc[-21]['sma150']})")
        return None

    latest_close = float(close.iloc[-1])
    sma50 = float(df.iloc[-1]['sma50'])
    sma150 = float(df.iloc[-1]['sma150'])
    sma150_prev20 = float(df.iloc[-21]['sma150'])
    year_high = float(high.tail(PRICE_HIGH_LOOKBACK).max())
    if year_high <= 0:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: year_high <= 0")
        return None

    # 趨勢結構：不再硬性要求，改為加分項
    trend_structure_ok = (latest_close > sma150 and sma150 > sma150_prev20)
    sma50_bonus = 0.0
    if latest_close > sma50 > sma150:
        sma50_bonus = 3.0

    # 價格相對 52 週高點：min_price_to_year_high_pct=0 時不限制
    min_price_to_year_high = MIN_PRICE_TO_YEAR_HIGH_PCT
    if DYNAMIC_ADJUSTMENT_ENABLED and len(df) >= ATR_PERIOD + 20:
        atr_series = calculate_atr(df, ATR_PERIOD)
        if not pd.isna(atr_series.iloc[-1]):
            atr_pct = (atr_series.iloc[-1] / latest_close) * 100
            if atr_pct > 3.0:
                min_price_to_year_high = max(0.55, MIN_PRICE_TO_YEAR_HIGH_PCT - 0.05)
            elif atr_pct < 1.5:
                min_price_to_year_high = min(0.75, MIN_PRICE_TO_YEAR_HIGH_PCT + 0.05)

    min_price_threshold = year_high * min_price_to_year_high
    if min_price_to_year_high > 0 and latest_close < min_price_threshold:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: price below {min_price_to_year_high:.0%} of year_high (close={latest_close:.2f}, year_high={year_high:.2f}, threshold={min_price_threshold:.2f})")
        return None

    avg_dollar_vol_20 = float((close.tail(20) * vol.tail(20)).mean())
    if not np.isfinite(avg_dollar_vol_20) or avg_dollar_vol_20 < 20_000_000:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: avg dollar vol < 20M ({avg_dollar_vol_20:,.0f})")
        return None

    recent = df.tail(CUP_LOOKBACK).reset_index(drop=True)
    if len(recent) < CUP_MIN_BARS + HANDLE_MIN_BARS + 20:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: recent too short ({len(recent)} < {CUP_MIN_BARS + HANDLE_MIN_BARS + 20})")
        return None

    right_search_end = len(recent) - HANDLE_MIN_BARS
    if right_search_end <= 60:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: right_search_end <= 60 ({right_search_end})")
        return None
    left_peak_idx = int(recent.iloc[: right_search_end - 20]['High'].astype(float).idxmax())
    left_peak = float(recent.iloc[left_peak_idx]['High'])
    right_search_start = max(left_peak_idx + 25, len(recent) // 2)
    if right_search_start >= right_search_end:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: right_search_start >= right_search_end ({right_search_start} >= {right_search_end})")
        return None
    right_peak_idx = int(recent.iloc[right_search_start:right_search_end]['High'].astype(float).idxmax())
    right_peak = float(recent.iloc[right_peak_idx]['High'])

    if right_peak < left_peak * RIM_ALIGNMENT_MIN or right_peak > left_peak * RIM_ALIGNMENT_MAX:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: rim alignment fail (left_peak={left_peak:.2f}, right_peak={right_peak:.2f}, min={RIM_ALIGNMENT_MIN}, max={RIM_ALIGNMENT_MAX})")
        return None
    if right_peak_idx - left_peak_idx < CUP_MIN_BARS:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: cup_min_bars fail (right_peak_idx={right_peak_idx}, left_peak_idx={left_peak_idx}, min={CUP_MIN_BARS})")
        return None

    cup_low_idx = int(recent.iloc[left_peak_idx:right_peak_idx + 1]['Low'].astype(float).idxmin())
    cup_low = float(recent.iloc[cup_low_idx]['Low'])
    cup_peak = max(left_peak, right_peak)
    if cup_peak <= 0 or cup_low >= cup_peak:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: invalid cup (cup_peak={cup_peak:.2f}, cup_low={cup_low:.2f})")
        return None
    cup_depth = (cup_peak - cup_low) / cup_peak
    if not (CUP_DEPTH_MIN <= cup_depth <= CUP_DEPTH_MAX):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: cup_depth={cup_depth:.2%} out of range [{CUP_DEPTH_MIN:.0%},{CUP_DEPTH_MAX:.0%}]")
        return None
    if cup_low_idx - left_peak_idx < 12 or right_peak_idx - cup_low_idx < 12:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: cup shape fail (left_to_low={cup_low_idx - left_peak_idx}, low_to_right={right_peak_idx - cup_low_idx})")
        return None

    bottom_left = max(left_peak_idx, cup_low_idx - 3)
    bottom_right = min(right_peak_idx, cup_low_idx + 3)
    bottom_zone = recent.iloc[bottom_left:bottom_right + 1]
    if len(bottom_zone) < 3 or int((bottom_zone['Low'].astype(float) <= cup_low * 1.05).sum()) < 3:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: bottom zone fail (len={len(bottom_zone)}, touches={(bottom_zone['Low'].astype(float) <= cup_low * 1.05).sum()})")
        return None

    handle = recent.iloc[right_peak_idx + 1:right_peak_idx + 1 + HANDLE_MAX_BARS].copy()
    if len(handle) < HANDLE_MIN_BARS:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: handle too short ({len(handle)} < {HANDLE_MIN_BARS})")
        return None
    handle_low_idx = int(handle['Low'].astype(float).idxmin())
    handle_low = float(handle.loc[handle_low_idx, 'Low'])
    handle_depth = (right_peak - handle_low) / right_peak if right_peak > 0 else 0.0

    # 严格柄检查
    if not (HANDLE_DEPTH_MIN <= handle_depth <= HANDLE_DEPTH_MAX):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: handle_depth={handle_depth:.2%} out of range [{HANDLE_DEPTH_MIN:.0%},{HANDLE_DEPTH_MAX:.0%}]")
        return None

    cup_mid = cup_low + 0.5 * (cup_peak - cup_low)
    handle_high = float(handle['High'].astype(float).max())
    if handle_high > cup_mid + (cup_peak - cup_low) * HANDLE_MAX_HEIGHT_PCT:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: handle_height fail (handle_high={handle_high:.2f}, cup_mid={cup_mid:.2f}, max_pct={HANDLE_MAX_HEIGHT_PCT})")
        return None

    if not HANDLE_ALLOW_BELOW_MIDLINE and handle_low < cup_mid:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: handle below midline (handle_low={handle_low:.2f}, cup_mid={cup_mid:.2f})")
        return None

    handle_range_pct = (handle_high - handle_low) / handle_low * 100
    if handle_range_pct > TIGHT_RANGE_MAX_PCT:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: handle_range_pct={handle_range_pct:.2f}% > max={TIGHT_RANGE_MAX_PCT}%")
        return None

    # 柄區域成交量：改為加分項，不再硬性要求 ≤ 50日均量 75%
    handle_vol = float(handle['Volume'].mean())
    avg_vol_50 = float(vol.tail(50).mean())
    handle_vol_dry = handle_vol <= avg_vol_50 * 0.75
    if not handle_vol_dry and stderr_path:
        append_log(stderr_path, f"DEBUG_CUP {symbol}: handle_vol not dry (handle_vol={handle_vol:.0f}, avg_vol_50={avg_vol_50:.0f}, ratio={handle_vol/avg_vol_50:.2f}) - no bonus")

    handle_days = int(handle_low_idx - right_peak_idx)
    if not (HANDLE_MIN_BARS <= handle_days <= HANDLE_MAX_BARS):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: handle_days={handle_days} out of range [{HANDLE_MIN_BARS},{HANDLE_MAX_BARS}]")
        return None

    cup_mid = cup_low + 0.5 * (cup_peak - cup_low)
    handle_below_mid = handle_low < cup_mid

    pivot = float(max(right_peak, handle['High'].astype(float).max()))
    if pivot <= 0:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: pivot <= 0")
        return None
    distance_to_pivot_pct = pct(latest_close, pivot)
    if distance_to_pivot_pct < NEAR_PIVOT_MIN_PCT or distance_to_pivot_pct > NEAR_PIVOT_MAX_PCT:
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: distance_to_pivot={distance_to_pivot_pct:.2f}% out of range [{NEAR_PIVOT_MIN_PCT},{NEAR_PIVOT_MAX_PCT}] (pivot={pivot:.2f})")
        return None

    # 高點收斂檢查 - 改為加分項
    high_prices = [left_peak, right_peak]
    highs_converging = (right_peak < left_peak)
    if not highs_converging and stderr_path:
        append_log(stderr_path, f"DEBUG_CUP {symbol}: highs not converging (left={left_peak:.2f}, right={right_peak:.2f}) - no bonus")

    # 波動率收縮檢查 - 改為加分項
    atr_80 = calculate_atr(df, 80).iloc[-1]
    atr_20 = calculate_atr(df, 20).iloc[-1]
    atr_contracted = False
    if not pd.isna(atr_80) and not pd.isna(atr_20) and atr_20 <= atr_80 * 0.85:
        atr_contracted = True
    elif stderr_path:
        append_log(stderr_path, f"DEBUG_CUP {symbol}: ATR not contracted (atr_20={atr_20:.4f}, atr_80={atr_80:.4f}, ratio={atr_20/atr_80:.2f}) - no bonus")

    avg_vol_10 = float(vol.tail(10).mean())
    avg_vol_20 = float(vol.tail(20).mean())
    avg_vol_50 = float(vol.tail(50).mean())
    volume_dry = avg_vol_10 < avg_vol_50 * VOLUME_DRY_THRESHOLD
    breakout_today = latest_close > pivot
    breakout_vol = float(vol.iloc[-1]) > avg_vol_20 * BREAKOUT_VOL_MULTIPLIER if avg_vol_20 > 0 else False
    near_pivot = NEAR_PIVOT_MIN_PCT <= distance_to_pivot_pct <= NEAR_PIVOT_MAX_PCT

    # 成交量/位置條件：只要滿足其一即可（不再硬性要求 volume_dry 或 breakout_vol）
    if not (breakout_today or volume_dry or near_pivot):
        if stderr_path:
            append_log(stderr_path, f"DEBUG_CUP {symbol}: volume/position condition fail (breakout_today={breakout_today}, volume_dry={volume_dry}, near_pivot={near_pivot}, vol10={avg_vol_10:.0f}, vol50={avg_vol_50:.0f}, threshold={VOLUME_DRY_THRESHOLD})")
        return None

    # 突破日成交量：不再硬性要求 1.5 倍
    if breakout_today:
        vol_ratio = float(vol.iloc[-1]) / avg_vol_20 if avg_vol_20 > 0 else 0
        # 不再因 vol_ratio < 1.5 而拒絕

    breakout_date = None
    if breakout_today:
        breakout_date = pd.Timestamp(df.iloc[-1]['Date'])

    handle_days = int(handle_low_idx - right_peak_idx)
    last_pullback_date = pd.Timestamp(recent.loc[handle_low_idx, 'Date'])
    stage = '剛突破' if breakout_today else ('近突破點' if near_pivot else '整理末端')

    if stage == '剛突破' and breakout_date is not None:
        days_since_breakout = (pd.Timestamp(df.iloc[-1]['Date']) - breakout_date).days
        if days_since_breakout > 5:
            if stderr_path:
                append_log(stderr_path, f"DEBUG_CUP {symbol}: days_since_breakout={days_since_breakout} > 5")
            return None

    score = CUP_BASE_SCORE
    score += max(0.0, 12.0 - abs(distance_to_pivot_pct) * CUP_DISTANCE_WEIGHT)
    score += max(0.0, 12.0 - cup_depth * CUP_DEPTH_WEIGHT)
    score += max(0.0, 10.0 - handle_depth * CUP_HANDLE_DEPTH_WEIGHT)
    score += min(10.0, pct(latest_close, sma50) * CUP_TREND_STRENGTH_WEIGHT)
    if handle_below_mid:
        score += HANDLE_BELOW_MIDLINE_PENALTY
    # 新增加分項（配合 config.yaml）
    if trend_structure_ok:
        score += 10.0  # trend_structure_bonus
    if atr_contracted:
        score += 8.0   # atr_contraction_bonus
    if highs_converging:
        score += 8.0   # highs_convergence_bonus
    if volume_dry:
        score += CUP_VOLUME_DRY_BONUS
    if handle_vol_dry:
        score += CUP_VOLUME_DRY_BONUS  # 柄區縮量同權重
    if breakout_today and breakout_vol:
        score += CUP_BREAKOUT_VOL_BONUS
    elif breakout_today:
        score += CUP_BREAKOUT_TODAY_BONUS
    if handle_days >= 5:
        score += CUP_HANDLE_DAYS_BONUS

    if stderr_path:
        append_log(stderr_path, f"DEBUG_CUP {symbol}: PASS stage={stage} score={score:.1f} cup_depth={cup_depth:.2%} handle_depth={handle_depth:.2%} handle_days={handle_days} pivot={pivot:.2f} dist={distance_to_pivot_pct:.2f}% vol_dry={volume_dry}")

    return {
        'symbol': symbol,
        'stage': stage,
        'pattern': '杯柄',
        'feature_label': f'柄{handle_days}天',
        'cup_depth_pct': round(cup_depth * 100.0, 1),
        'handle_depth_pct': round(handle_depth * 100.0, 1),
        'handle_days': handle_days,
        'pivot': round(pivot, 2),
        'price': round(latest_close, 2),
        'distance_to_pivot_pct': round(distance_to_pivot_pct, 2),
        'volume_feature': '量縮乾淨' if volume_dry else ('突破放量' if breakout_today and breakout_vol else '一般'),
        'last_pullback_date': last_pullback_date.strftime('%Y-%m-%d'),
        'breakout_date': breakout_date.strftime('%Y-%m-%d') if breakout_date is not None else '',
        'avg_20d_dollar_volume_m': round(avg_dollar_vol_20 / 1_000_000, 1),
        'score': round(score, 1),
        '_sort_score': score,
        '_sort_breakout_date': breakout_date.isoformat() if breakout_date is not None else '',
        '_sort_pullback_date': last_pullback_date.isoformat(),
    }

def detect_best_pattern(symbol: str, df: pd.DataFrame, stderr_path: str = None):
    candidates = []
    for detector in (detect_vcp, detect_cup_handle):
        row = detector(symbol, df, stderr_path)
        if row:
            candidates.append(row)
    if not candidates:
        return None
    if len(candidates) >= 2:
        patterns = [c.get('pattern') for c in candidates]
        best = sorted(
            candidates,
            key=lambda x: (x.get('_sort_score', 0.0), 1 if x.get('pattern') == '杯柄' else 0),
            reverse=True,
        )[0]
        best['pattern'] = 'BOTH'
        best['matched_patterns'] = patterns
        return best
    best = sorted(
        candidates,
        key=lambda x: (x.get('_sort_score', 0.0), 1 if x.get('pattern') == '杯柄' else 0),
        reverse=True,
    )[0]
    matched = [x.get('pattern') for x in candidates if x.get('pattern')]
    best['matched_patterns'] = matched
    return best

def pattern_code(row: dict) -> str:
    matched = set(row.get('matched_patterns') or [])
    if {'VCP', '杯柄'}.issubset(matched):
        return 'BOTH'
    pattern = row.get('pattern', '')
    if pattern == 'VCP':
        return 'VCP'
    if pattern == '杯柄':
        return 'CUP'
    return '-'

def pattern_priority(row: dict) -> int:
    code = pattern_code(row)
    if code == 'BOTH':
        return 3
    if code == 'VCP':
        return 2
    if code == 'CUP':
        return 1
    return 0

def latest_signal_date(row: dict) -> str:
    breakout = str(row.get('_sort_breakout_date') or '')
    pullback = str(row.get('_sort_pullback_date') or '')
    return max(breakout, pullback)

def sort_key(row: dict):
    return (
        latest_signal_date(row),
        pattern_priority(row),
        str(row.get('_sort_breakout_date') or ''),
        str(row.get('_sort_pullback_date') or ''),
        float(row.get('_sort_score') or 0.0),
    )

def feature_code(row: dict) -> str:
    pattern = row.get('pattern', '')
    if pattern == 'VCP':
        return f"{int(row.get('contraction_count', 0))}xCT"
    if pattern == '杯柄':
        return f"H{int(row.get('handle_days', 0)):02d}d"
    feature = str(row.get('feature_label', '-'))
    return feature[:8]

def render_stage_table(lines, title: str, icon: str, rows: list[dict]):
    lines.append(f'### {icon} {title}')
    lines.append('')
    if not rows:
        lines.append('今日沒有符合條件的標的。')
        lines.append('')
        return
    lines.append('```text')
    lines.append('代碼     PAT   整理日   突破日')
    lines.append('-------  ----  -------  -------')
    for row in rows:
        symbol = str(row.get('symbol', '-'))[:7].ljust(7)
        pat = pattern_code(row).ljust(4)
        pullback = str(row.get('last_pullback_date', ''))[5:] or '-'
        breakout = (str(row.get('breakout_date', ''))[5:] if row.get('breakout_date') else '-')
        lines.append(f'{symbol}  {pat}  {pullback:<7}  {breakout:<7}')
    lines.append('```')
    lines.append('PAT: BOTH=雙重共振｜VCP=波幅收縮｜CUP=杯柄')
    lines.append('')

def filter_rows_by_pat(rows: list[dict], code: str) -> list[dict]:
    return [row for row in rows if pattern_code(row) == code]

def exclude_symbols(rows: list[dict], excluded_symbols: set[str]) -> list[dict]:
    return [row for row in rows if str(row.get('symbol', '')) not in excluded_symbols]

def render_markdown(out: dict) -> str:
    lines = []
    data_date = str(out.get('generated_at_utc', ''))[:10]
    lines.append('# 📈 美股 VCP + 杯柄形態掃描')
    lines.append('')
    lines.append('🔎 數據來源：Nasdaq Trader 股票/ETF 名單 + Yahoo Finance / yfinance 日線 OHLCV')
    lines.append(f'📅 數據日期：{data_date}')

    # 視覺分析覆蓋率統計
    all_results = out.get('results', [])
    visual_ok = 0
    visual_total = 0
    for row in all_results:
        va = row.get('visual_analysis')
        if va:
            visual_total += 1
            if isinstance(va, dict) and 'error' not in va:
                visual_ok += 1
    if visual_total > 0:
        lines.append(f'👁️ 視覺分析覆蓋率：{visual_ok}/{visual_total}')
    lines.append('')

    lines.append(f"📌 摘要：掃描 {out.get('universe_total', 0)}｜流動性通過 {out.get('liquid_count', 0)}｜深掃 {out.get('deep_scan_count', 0)}｜候選 {out.get('candidate_total', 0)}")
    lines.append(f"🧩 缺口：stage1 misses {out.get('stage1_misses', 0)}｜stage2 misses {out.get('stage2_misses', 0)}")
    lines.append('')
    lines.append('')

    # 從完整 results 篩選各模式，而非只用 top10
    all_results = out.get('results', [])

    both_rows = [row for row in all_results if pattern_code(row) == 'BOTH']
    vcp_rows = [row for row in all_results if pattern_code(row) == 'VCP']
    cup_rows = [row for row in all_results if pattern_code(row) == 'CUP']

    both_near_rows = [row for row in both_rows if row.get('stage') == '近突破點']
    both_breakout_rows = [row for row in both_rows if row.get('stage') == '剛突破']

    vcp_near_rows = [row for row in vcp_rows if row.get('stage') == '近突破點']
    vcp_breakout_rows = [row for row in vcp_rows if row.get('stage') == '剛突破']

    cup_near_rows = [row for row in cup_rows if row.get('stage') == '近突破點']
    cup_breakout_rows = [row for row in cup_rows if row.get('stage') == '剛突破']

    early_rows = out.get('top10_early_setup', []) or []
    lines.append('## 整理末端（觀察區）')
    lines.append('')
    render_stage_table(lines, '前10名', '🔍', early_rows[:10])
    lines.append('')

    if not vcp_near_rows and not vcp_breakout_rows and not both_near_rows and not both_breakout_rows and not cup_near_rows and not cup_breakout_rows:
        lines.append('今日沒有篩出符合條件的 雙重共振 / VCP / CUP 候選。')
        lines.append('')
        lines.append('⚠️ 風險提示：這是 AI 掃描出的參考買賣點，不涉及投資建議，做多有風險。')
        return '\n'.join(lines)

    raw_candidate_total = out.get('raw_candidate_total', out.get('candidate_total', 0))
    filtered_out = max(0, raw_candidate_total - out.get('candidate_total', 0))
    if filtered_out > 0:
        lines.append(f"（原始 {raw_candidate_total}，質量過濾淘汰 {filtered_out}）")
        lines.append('')
    lines.append('## 雙重共振')
    lines.append('')
    render_stage_table(lines, '近突破點前10名', '🎯', both_near_rows[:10])
    render_stage_table(lines, '剛突破前10名', '🚀', both_breakout_rows[:10])
    lines.append('## VCP')
    lines.append('')
    render_stage_table(lines, '近突破點前10名', '🎯', vcp_near_rows[:10])
    render_stage_table(lines, '剛突破前10名', '🚀', vcp_breakout_rows[:10])
    lines.append('## CUP')
    lines.append('')
    render_stage_table(lines, '近突破點前10名', '🎯', cup_near_rows[:10])
    render_stage_table(lines, '剛突破前10名', '🚀', cup_breakout_rows[:10])
    lines.append('⚠️ 風險提示：這是 AI 掃描出的參考買賣點，不涉及投資建議，做多有風險。')
    return '\n'.join(lines)

# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='U.S. VCP market scan')
    parser.add_argument('--format', choices=['json', 'markdown'], default='markdown')
    parser.add_argument('--max-symbols', type=int, default=0)
    parser.add_argument('--universe-shard-count', type=int, default=1)
    parser.add_argument('--universe-shard-index', type=int, default=0)
    parser.add_argument('--nasdaq-listed-path', default=os.environ.get('HERMES_VCP_NASDAQ_LISTED_PATH', ''))
    parser.add_argument('--other-listed-path', default=os.environ.get('HERMES_VCP_OTHER_LISTED_PATH', ''))
    parser.add_argument('--symbols-file', default=os.environ.get('HERMES_VCP_SYMBOLS_FILE', ''))
    parser.add_argument('--stderr-path', default='/tmp/us_vcp_scan_yf_stderr.log')
    parser.add_argument('--shards', type=int, default=int(os.environ.get('HERMES_VCP_SHARDS', '4')))
    parser.add_argument('--artifact-dir', default=os.environ.get('HERMES_VCP_ARTIFACT_DIR', ''))
    parser.add_argument('--stage1-period', default=os.environ.get('HERMES_VCP_STAGE1_PERIOD', '20d'))
    parser.add_argument('--stage1-batch', type=int, default=int(os.environ.get('HERMES_VCP_STAGE1_BATCH', '90')))
    parser.add_argument('--stage2-batch', type=int, default=int(os.environ.get('HERMES_VCP_STAGE2_BATCH', '100')))
    parser.add_argument('--worker-dir', default=os.environ.get('HERMES_VCP_WORKER_DIR', ''))
    args = parser.parse_args()

    stderr_path = args.stderr_path
    open(stderr_path, 'w').close()
    artifact_dir = Path(args.artifact_dir).expanduser() if args.artifact_dir else Path(stderr_path).resolve().parent / (Path(stderr_path).stem + '.artifacts')
    artifact_dir.mkdir(parents=True, exist_ok=True)

    append_log(stderr_path, f"SCAN_START report=VCP format={args.format} max_symbols={args.max_symbols or 'all'} shards={max(1, args.shards)} universe_shard={args.universe_shard_index + 1}/{max(1, args.universe_shard_count)} stage1_period={args.stage1_period} stage1_batch={args.stage1_batch} stage2_batch={args.stage2_batch}")

    if args.symbols_file:
        original_symbols = [line.strip() for line in Path(args.symbols_file).read_text(encoding='utf-8').splitlines() if line.strip()]
    else:
        nasdaq_text = Path(args.nasdaq_listed_path).read_text(encoding='utf-8') if args.nasdaq_listed_path else fetch_text(NASDAQ_LISTED_URL)
        other_text = Path(args.other_listed_path).read_text(encoding='utf-8') if args.other_listed_path else fetch_text(OTHER_LISTED_URL)
        nasdaq = parse_nasdaq_listed(nasdaq_text)
        other = parse_other_listed(other_text)
        uni = pd.concat([nasdaq, other], ignore_index=True)
        uni = uni.drop_duplicates(subset=['Symbol']).reset_index(drop=True)
        uni['keep'] = uni.apply(lambda r: is_regular_security(r['Symbol'], r['name'], bool(r['etf']), bool(r['test_issue'])), axis=1)
        uni = uni[uni['keep']].copy()
        if args.max_symbols and args.max_symbols > 0:
            uni = uni.head(args.max_symbols).copy()
        if args.universe_shard_count > 1:
            symbol_shards = split_into_shards(uni['Symbol'].tolist(), args.universe_shard_count)
            shard_index = max(0, min(args.universe_shard_index, len(symbol_shards) - 1)) if symbol_shards else 0
            selected_symbols = set(symbol_shards[shard_index]) if symbol_shards else set()
            uni = uni[uni['Symbol'].isin(selected_symbols)].copy().reset_index(drop=True)
        original_symbols = uni['Symbol'].tolist()
    mapped = {yahoo_symbol(sym): sym for sym in original_symbols}
    yahoo_symbols = list(mapped.keys())

    append_log(stderr_path, f"STAGE1_START universe={len(yahoo_symbols)}")
    stage1, miss1 = download_bars(yahoo_symbols, args.stage1_period, stderr_path, batch=args.stage1_batch, phase='STAGE1')
    liquid = []
    for ys, xdf in stage1.items():
        x = xdf.dropna(subset=['Close', 'Volume']).reset_index(drop=True)
        if len(x) == 0:
            continue
        avg_dollar_vol_20d = trailing_avg_dollar_volume(x, len(x) - 1, days=20)
        if avg_dollar_vol_20d is not None and avg_dollar_vol_20d >= 20_000_000:
            liquid.append(ys)
    append_log(stderr_path, f"STAGE1_DONE ok={len(stage1)} liquid={len(liquid)} misses={len(miss1)}")

    (artifact_dir / 'liquid_symbols.json').write_text(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'universe_total': len(yahoo_symbols),
        'liquid_count': len(liquid),
        'liquid_symbols': liquid,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    shard_lists = split_into_shards(liquid, max(1, args.shards))
    results = []
    miss2 = set()
    deep_scan_count = 0
    shard_summaries = []

    for shard_idx, shard_symbols in enumerate(shard_lists, start=1):
        append_log(stderr_path, f"STAGE2_SHARD_START shard={shard_idx}/{len(shard_lists)} symbols={len(shard_symbols)}")
        stage2, shard_miss = download_bars(shard_symbols, '10mo', stderr_path, batch=args.stage2_batch, phase=f'STAGE2_SHARD_{shard_idx:02d}')

        # 儲存 stage2 原始資料供視覺分析使用
        import pickle
        stage2_pickle_path = artifact_dir / f'shard_{shard_idx:02d}_data.pkl'
        with open(stage2_pickle_path, 'wb') as f:
            pickle.dump(stage2, f)

        shard_rows = []
        for ys, df in stage2.items():
            try:
                row = detect_best_pattern(mapped[ys], df, stderr_path)
                if row:
                    if QUALITY_FILTER_ENABLED:
                        stage = row.get('stage', '整理末端')
                        min_score = QUALITY_MIN_SCORE_BY_STAGE.get(stage, DEFAULT_QUALITY_MIN_SCORE)
                        score = row.get('score', 0)
                        if score < min_score:
                            append_log(stderr_path, f"QUALITY_FILTER {ys} stage={stage} score={score:.1f} < {min_score}")
                            continue
                    shard_rows.append(row)
            except Exception as e:
                append_log(stderr_path, f"SCAN_ERROR {ys} {e}\\n{traceback.format_exc()}")
        results.extend(shard_rows)
        deep_scan_count += len(stage2)
        miss2.update(shard_miss)
        shard_summary = {
            'shard': shard_idx,
            'input_symbols': len(shard_symbols),
            'downloaded_symbols': len(stage2),
            'misses': len(shard_miss),
            'candidates': len(shard_rows),
        }
        shard_summaries.append(shard_summary)
        (artifact_dir / f'shard_{shard_idx:02d}.json').write_text(json.dumps({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'summary': shard_summary,
            'results': shard_rows,
            'miss_symbols': sorted(list(shard_miss)),
        }, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        append_log(stderr_path, f"STAGE2_SHARD_DONE shard={shard_idx}/{len(shard_lists)} downloaded={len(stage2)} misses={len(shard_miss)} candidates={len(shard_rows)}")

    results.sort(key=sort_key, reverse=True)
    raw_candidate_total = len(results)

    both_rows = sorted([row for row in results if row.get('stage') == '雙重共振'], key=sort_key, reverse=True)
    near_rows = sorted([row for row in results if row.get('stage') == '近突破點'], key=sort_key, reverse=True)
    breakout_rows = sorted([row for row in results if row.get('stage') == '剛突破'], key=sort_key, reverse=True)
    early_rows = sorted([row for row in results if row.get('stage') == '整理末端'], key=sort_key, reverse=True)

    top10 = results[:10]
    top10_near_pivot = near_rows[:10]
    top10_breakout = breakout_rows[:10]
    top10_early_setup = early_rows[:10]
    top10_both = both_rows[:10]

    out = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'data_sources': [
            'Nasdaq Trader nasdaqlisted.txt',
            'Nasdaq Trader otherlisted.txt',
            'Yahoo Finance / yfinance 日線 OHLCV',
            'VCP / 杯柄 = 上升趨勢 + 收縮整理結構；VCP 側重多次波幅收窄，杯柄側重圓弧杯身 + 上半部短柄 + 接近/突破樞軸位',
        ],
        'universe_total': int(len(original_symbols)),
        'liquid_count': int(len(liquid)),
        'deep_scan_count': int(deep_scan_count),
        'stage1_misses': int(len(miss1)),
        'stage2_misses': int(len(miss2)),
        'candidate_total': int(len(results)),
        'raw_candidate_total': int(raw_candidate_total),
        'stderr_log': stderr_path,
        'artifact_dir': str(artifact_dir),
        'universe_shard_count': int(args.universe_shard_count),
        'universe_shard_index': int(args.universe_shard_index),
        'shard_count': len(shard_lists),
        'shards': shard_summaries,
        'results': results,
        'top10': top10,
        'top10_near_pivot': top10_near_pivot,
        'top10_breakout': top10_breakout,
        'top10_early_setup': top10_early_setup,
        'top10_both': top10_both,
    }
    (artifact_dir / 'final_output.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    append_log(stderr_path, f"SCAN_DONE report=VCP deep_scan={deep_scan_count} candidates={len(results)}")
    if args.format == 'markdown':
        print(render_markdown(out))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))

if __name__ == '__main__':
    main()
