#!/usr/bin/env python3
"""
美股動量排名掃描器
計算 20R/60R/120R/Rank 並分三類輸出：
1. 20R和60R同時在75-89但120R在80以下
2. 20R和60R同時在90以上但120R在80以下
3. 總Rank >= 90
排序：按 Rank 從高到低
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

# 動量窗口
MOMENTUM_WINDOWS = [20, 60, 120]
SPY_SYMBOL = "SPY"

# 掃描參數
MIN_LOOKBACK_DAYS = 252  # 至少需要 1 年數據
LIQUIDITY_THRESHOLD = 20_000_000  # 20日均交易額門檻


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
BAD_SYMBOL_SUFFIXES = ('-V', '.V', '-WI', '.WI', '-WD', '.WD', '-WS', '.WS', '-W', '.W', '-U', '.U', '-R', '.R', '-RT', '.RT', '-P', '.P')
BAD_SYMBOL_SUBSTRINGS = ('^', '/', '=')

DEFAULT_BAD_SYMBOLS_FILE = Path(__file__).resolve().parent / 'data' / 'universe' / 'yahoo_bad_symbols.txt'

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


def download_bars(symbols, period, stderr_path, batch=200, phase='DOWNLOAD'):
    frames = {}
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
                    45,
                    lambda: yf.download(
                        tickers=tickers,
                        period=period,
                        interval='1d',
                        auto_adjust=False,
                        group_by='ticker',
                        progress=False,
                        threads=False,
                        prepost=False,
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
                    if len(group) == 1:
                        sym = group[0]
                        df = data.copy()
                        df.columns = [c[0] for c in df.columns]
                        df = df.reset_index().rename(columns={df.index.name or 'Date': 'Date'})
                        frames[sym] = df
                    else:
                        for sym in group:
                            try:
                                sdf = data[sym].reset_index()
                                frames[sym] = sdf
                            except Exception:
                                misses.add(sym)
                    continue
                for sym in group:
                    try:
                        sdf = data[sym].copy().reset_index()
                        if len(sdf.dropna(how='all')) == 0:
                            misses.add(sym)
                        else:
                            frames[sym] = sdf
                    except Exception:
                        misses.add(sym)
            else:
                misses.update(group)
        else:
            if len(group) == 1:
                sdf = data.reset_index()
                frames[group[0]] = sdf
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

    # 清理並標準化
    out = {}
    for sym, df in frames.items():
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
        # 修正2: 避免 Close 全為 NaN 導致計算錯誤
        if sdf['Close'].isnull().all():
            misses.add(sym)
            continue
        sdf['Date'] = pd.to_datetime(sdf['Date']).dt.tz_localize(None)
        out[sym] = sdf.reset_index(drop=True)
    return out, misses


def calculate_momentum_ranks(price_data: dict, spy_data: pd.DataFrame) -> pd.DataFrame:
    """
    計算所有標的的動量排名
    Returns DataFrame with columns: Symbol, Price, 1D%, 20R, 60R, 120R, Rank, REL20, REL60, REL120
    """
    # 計算 SPY 的各窗口報酬率
    spy_close = spy_data['Close'].astype(float).values
    if len(spy_close) < max(MOMENTUM_WINDOWS) + 5:
        raise ValueError("SPY 數據不足")
    
    spy_returns = {}
    for window in MOMENTUM_WINDOWS:
        if len(spy_close) > window:
            spy_returns[window] = (spy_close[-1] / spy_close[-window-1] - 1.0) * 100.0
        else:
            spy_returns[window] = 0.0

    # 計算每個標的的報酬率和超額報酬
    results = []
    for sym, df in price_data.items():
        if len(df) < max(MOMENTUM_WINDOWS) + 5:
            continue
        close = df['Close'].astype(float).values
        if len(close) < max(MOMENTUM_WINDOWS) + 5:
            continue
        
        # 當日價格和漲跌幅
        price_today = float(close[-1])
        price_prev = float(close[-2])
        pct_1d = (price_today / price_prev - 1.0) * 100.0
        
        # 各窗口報酬率
        returns = {}
        excess_returns = {}
        for window in MOMENTUM_WINDOWS:
            if len(close) > window:
                ret = (close[-1] / close[-window-1] - 1.0) * 100.0
                returns[window] = ret
                excess_returns[window] = ret - spy_returns[window]
            else:
                returns[window] = 0.0
                excess_returns[window] = 0.0
        
        results.append({
            'Symbol': sym,
            'Price': round(price_today, 2),
            '1D%': round(pct_1d, 2),
            'REL20': round(excess_returns[20], 2),
            'REL60': round(excess_returns[60], 2),
            'REL120': round(excess_returns[120], 2),
            'excess_20': excess_returns[20],
            'excess_60': excess_returns[60],
            'excess_120': excess_returns[120],
        })
    
    if not results:
        return pd.DataFrame()
    
    df_results = pd.DataFrame(results)
    
    # 計算百分位排名 (1-99, 越大越強)
    for window in MOMENTUM_WINDOWS:
        col_excess = f'excess_{window}'
        col_rank = f'{window}R'
        # 使用 rank(pct=True) 得到 0-1 百分位，再轉為 1-99
        df_results[col_rank] = (df_results[col_excess].rank(pct=True, method='min') * 99).clip(1, 99).round(0).astype(int)
    
    # 計算綜合 Rank = 0.2*20R + 0.4*60R + 0.4*120R
    df_results['Rank'] = (0.2 * df_results['20R'] + 0.4 * df_results['60R'] + 0.4 * df_results['120R']).round(1)
    
    # 排序：Rank 從高到低
    df_results = df_results.sort_values('Rank', ascending=False).reset_index(drop=True)
    
    # 只保留需要的欄位
    output_cols = ['Symbol', 'Price', '1D%', '20R', '60R', '120R', 'Rank', 'REL20', 'REL60', 'REL120']
    return df_results[output_cols]


def filter_categories(df: pd.DataFrame) -> dict:
    """依照三個條件分類"""
    # 1. 20R和60R同時在75-89但120R在80以下
    cat1 = df[
        (df['20R'] >= 75) & (df['20R'] <= 89) &
        (df['60R'] >= 75) & (df['60R'] <= 89) &
        (df['120R'] < 80)
    ].copy()
    
    # 2. 20R和60R同時在90以上但120R在80以下
    cat2 = df[
        (df['20R'] >= 90) &
        (df['60R'] >= 90) &
        (df['120R'] < 80)
    ].copy()
    
    # 3. 總Rank >= 90
    cat3 = df[df['Rank'] >= 90].copy()
    
    # 都按 Rank 從高到低排序
    for cat in [cat1, cat2, cat3]:
        cat.sort_values('Rank', ascending=False, inplace=True)
    
    return {
        'category1_20R60R_75_89_120R_lt80': cat1,
        'category2_20R60R_ge90_120R_lt80': cat2,
        'category3_rank_ge90': cat3,
    }


def generate_report(categories: dict, scan_info: dict) -> str:
    """生成 Markdown 簡報"""
    lines = []
    
    lines.append("📊 **美股動量排名週報**")
    lines.append("")
    lines.append(f"📅 掃描日期：{scan_info.get('scan_date', '未知')}")
    lines.append(f"🔢 掃描標的數：{scan_info.get('universe_total', 0)}")
    lines.append(f"✅ 有效數據：{scan_info.get('valid_count', 0)}")
    lines.append(f"📈 數據來源：Nasdaq Trader + Yahoo Finance (yfinance)")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # 類別 1
    cat1 = categories['category1_20R60R_75_89_120R_lt80']
    lines.append(f"## 🟡 類別 1：20R&60R在 75-89，但 120R < 80 （共 {len(cat1)} 檔）")
    lines.append("")
    if len(cat1) > 0:
        lines.append("| 代碼 | 20R | 60R | 120R | Rank |")
        lines.append("|------|-----|-----|------|------|")
        for _, row in cat1.iterrows():
            lines.append(f"| {row['Symbol']} | {int(row['20R'])} | {int(row['60R'])} | {int(row['120R'])} | {row['Rank']:.1f} |")
    else:
        lines.append("*無符合條件標的*")
    lines.append("")
    
    # 類別 2
    cat2 = categories['category2_20R60R_ge90_120R_lt80']
    lines.append(f"## 🟢 類別 2：20R&60R ≥ 90，但 120R < 80 （共 {len(cat2)} 檔）")
    lines.append("")
    if len(cat2) > 0:
        lines.append("| 代碼 | 20R | 60R | 120R | Rank |")
        lines.append("|------|-----|-----|------|------|")
        for _, row in cat2.iterrows():
            lines.append(f"| {row['Symbol']} | {int(row['20R'])} | {int(row['60R'])} | {int(row['120R'])} | {row['Rank']:.1f} |")
    else:
        lines.append("*無符合條件標的*")
    lines.append("")
    
    # 類別 3
    cat3 = categories['category3_rank_ge90']
    lines.append(f"## 🔵 類別 3：總 Rank ≥ 90 （共 {len(cat3)} 檔）")
    lines.append("")
    if len(cat3) > 0:
        lines.append("| 代碼 | 20R | 60R | 120R | Rank |")
        lines.append("|------|-----|-----|------|------|")
        for _, row in cat3.iterrows():
            lines.append(f"| {row['Symbol']} | {int(row['20R'])} | {int(row['60R'])} | {int(row['120R'])} | {row['Rank']:.1f} |")
    else:
        lines.append("*無符合條件標的*")
    lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append("⚠️ **風險提示**：此為動量排名篩選結果，非買賣建議。排名基於相對 SPY 的超額報酬百分位，數值越大代表相對動量越強。請自行判斷風險。")
    
    return "\n".join(lines)


def generate_discord_embed(categories: dict, scan_info: dict) -> dict:
    """生成 Discord Embed JSON"""
    cat1 = categories['category1_20R60R_75_89_120R_lt80']
    cat2 = categories['category2_20R60R_ge90_120R_lt80']
    cat3 = categories['category3_rank_ge90']
    
    def format_category(df, title, color):
        if len(df) == 0:
            return {"name": title, "value": "無符合條件標的", "inline": False}
        
        # 限制顯示前 20 檔
        display_df = df.head(20)
        lines = []
        for _, row in display_df.iterrows():
            lines.append(f"`{row['Symbol']}` 20R:{int(row['20R'])} 60R:{int(row['60R'])} 120R:{int(row['120R'])} Rank:{row['Rank']:.1f}")
        
        value = "\n".join(lines)
        if len(df) > 20:
            value += f"\n... 共 {len(df)} 檔，僅顯示前 20"
        
        return {"name": title, "value": value, "inline": False}
    
    embed = {
        "title": "📊 美股動量排名週報",
        "description": f"掃描日期：{scan_info.get('scan_date', '未知')} | 標的：{scan_info.get('valid_count', 0)}/{scan_info.get('universe_total', 0)}",
        "color": 0x3498db,
        "fields": [
            format_category(cat1, "🟡 類別1：20R&60R 75-89, 120R<80", 0xf39c12),
            format_category(cat2, "🟢 類別2：20R&60R ≥90, 120R<80", 0x27ae60),
            format_category(cat3, "🔵 類別3：Rank ≥ 90", 0x3498db),
        ],
        "footer": {"text": "相對 SPY 超額報酬百分位排名 | 非投資建議"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    return {"embeds": [embed]}


def main():
    parser = argparse.ArgumentParser(description='U.S. Momentum Rank Scanner')
    parser.add_argument('--format', choices=['json', 'markdown', 'discord'], default='json')
    parser.add_argument('--max-symbols', type=int, default=0, help='Optional cap on universe size for smoke tests')
    parser.add_argument('--stderr-path', default='/tmp/momentum_scan_stderr.log')
    parser.add_argument('--shards', type=int, default=int(os.environ.get('MOMENTUM_SCAN_SHARDS', '4')))
    parser.add_argument('--artifact-dir', default=os.environ.get('MOMENTUM_SCAN_ARTIFACT_DIR', ''))
    parser.add_argument('--stage1-period', default=os.environ.get('MOMENTUM_SCAN_STAGE1_PERIOD', '1mo'))
    parser.add_argument('--stage1-batch', type=int, default=int(os.environ.get('MOMENTUM_SCAN_STAGE1_BATCH', '120')))
    parser.add_argument('--stage2-batch', type=int, default=int(os.environ.get('MOMENTUM_SCAN_STAGE2_BATCH', '100')))
    parser.add_argument('--stage2-period', default=os.environ.get('MOMENTUM_SCAN_STAGE2_PERIOD', '1y'))
    args = parser.parse_args()

    stderr_path = args.stderr_path
    open(stderr_path, 'w').close()
    artifact_dir = Path(args.artifact_dir).expanduser() if args.artifact_dir else Path(stderr_path).resolve().parent / (Path(stderr_path).stem + '.artifacts')
    artifact_dir.mkdir(parents=True, exist_ok=True)

    scan_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    append_log(stderr_path, f"MOMENTUM_SCAN_START format={args.format} max_symbols={args.max_symbols or 'all'} shards={max(1, args.shards)} stage1_period={args.stage1_period}")

    # 1. 獲取股票池
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
    
    # 確保 SPY 在列表中
    if 'SPY' not in yahoo_symbols:
        yahoo_symbols.append('SPY')
        mapped['SPY'] = 'SPY'
    
    append_log(stderr_path, f"UNIVERSE_TOTAL={len(original_symbols)} (含SPY)")

    # 2. 階段1：流動性篩選 (1mo 數據)
    stage1, miss1 = download_bars(yahoo_symbols, args.stage1_period, stderr_path, batch=args.stage1_batch, phase='STAGE1')
    liquid = []
    for ys, df in stage1.items():
        x = df.dropna(subset=['Close', 'Volume']).reset_index(drop=True)
        if len(x) == 0:
            continue
        avg_dollar_vol_20d = float((x['Close'].astype(float) * x['Volume'].astype(float)).tail(20).mean())
        if avg_dollar_vol_20d >= LIQUIDITY_THRESHOLD:
            liquid.append(ys)
    append_log(stderr_path, f"STAGE1_DONE ok={len(stage1)} liquid={len(liquid)} misses={len(miss1)}")

    # 3. 階段2：下載 深度數據用於動量計算
    shard_lists = split_into_shards(liquid, max(1, args.shards))
    all_price_data = {}
    miss2 = set()
    deep_scan_count = 0
    stage2_period = getattr(args, 'stage2_period', '1y')

    for shard_idx, shard_symbols in enumerate(shard_lists, start=1):
        append_log(stderr_path, f"STAGE2_SHARD_START shard={shard_idx}/{len(shard_lists)} symbols={len(shard_symbols)}")
        stage2, shard_miss = download_bars(shard_symbols, stage2_period, stderr_path, batch=args.stage2_batch, phase=f'STAGE2_SHARD_{shard_idx:02d}')
        deep_scan_count += len(stage2)
        miss2.update(shard_miss)
        
        # 過濾數據長度足夠的
        for ys, df in stage2.items():
            if len(df) >= MIN_LOOKBACK_DAYS:
                all_price_data[ys] = df
        
        append_log(stderr_path, f"STAGE2_SHARD_DONE shard={shard_idx} downloaded={len(stage2)} valid={len([d for d in stage2.values() if len(d)>=MIN_LOOKBACK_DAYS])} misses={len(shard_miss)}")

    # 4. 計算動量排名
    spy_data = all_price_data.get('SPY')
    if spy_data is None:
        append_log(stderr_path, "ERROR: SPY data not available!")
        return
    
    # 移除 SPY 不參與排名
    scan_data = {k: v for k, v in all_price_data.items() if k != 'SPY'}
    
    append_log(stderr_path, f"MOMENTUM_CALC_START symbols={len(scan_data)}")
    momentum_df = calculate_momentum_ranks(scan_data, spy_data)
    append_log(stderr_path, f"MOMENTUM_CALC_DONE ranked={len(momentum_df)}")

    # 5. 分類
    categories = filter_categories(momentum_df)

    # 6. 準備輸出
    scan_info = {
        'scan_date': scan_date,
        'universe_total': len(original_symbols),
        'liquid_count': len(liquid),
        'valid_count': len(momentum_df),
        'stage1_misses': len(miss1),
        'stage2_misses': len(miss2),
        'cat1_count': len(categories['category1_20R60R_75_89_120R_lt80']),
        'cat2_count': len(categories['category2_20R60R_ge90_120R_lt80']),
        'cat3_count': len(categories['category3_rank_ge90']),
    }

    # 輸出 JSON 給後續處理
    output_json = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'scan_info': scan_info,
        'full_rankings': momentum_df.to_dict('records'),
        'categories': {k: v.to_dict('records') for k, v in categories.items()},
    }
    
    (artifact_dir / 'momentum_rank_output.json').write_text(json.dumps(output_json, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

    # 根據格式輸出
    if args.format == 'markdown':
        report = generate_report(categories, scan_info)
        (artifact_dir / 'momentum_rank_report.md').write_text(report, encoding='utf-8')
        print(report)
    elif args.format == 'discord':
        discord_payload = generate_discord_embed(categories, scan_info)
        (artifact_dir / 'momentum_discord_embed.json').write_text(json.dumps(discord_payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(discord_payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(output_json, ensure_ascii=False, indent=2, default=str))

    append_log(stderr_path, f"MOMENTUM_SCAN_DONE cat1={scan_info['cat1_count']} cat2={scan_info['cat2_count']} cat3={scan_info['cat3_count']}")


if __name__ == '__main__':
    main()