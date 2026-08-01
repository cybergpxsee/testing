#!/usr/bin/env python3
"""
Monthly Universe Update Script - Production Version
使用 curl_cffi 模擬瀏覽器指紋，避免 Yahoo Finance 封禁
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 導入 curl_cffi 版的 Yahoo 下載器
try:
    from yahoo_fetcher import download_bars as download_daily_bars, fetch_yahoo_chart
    USE_CURL_CFFI = True
except ImportError:
    USE_CURL_CFFI = False
    import yfinance as yf

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
# Shared repository for universe data
SHARED_REPO_BASE = "https://raw.githubusercontent.com/cybergpxsee/data-share/main"
SHARED_UNIVERSE_BASE = f"{SHARED_REPO_BASE}/data/universe"
UA = "Mozilla/5.0 (X11; Linux x86_64) UniverseUpdater/1.0"
MIN_AVG_DOLLAR_VOL_30D = 15_000_000
DEFAULT_PERIOD = '2mo'
DEFAULT_BATCH = 80
DEFAULT_SHARD_COUNT = 4
MANUAL_EXCLUSION_FILENAME = 'exclude_symbols.txt'
MONTHLY_EXCLUSION_FILENAME = 'monthly_excluded_symbols.json'
REQUIRED_CACHE_FILES = (
    'nasdaqlisted.txt', 'otherlisted.txt', 'us_symbols.csv',
    'monthly_excluded_symbols.json', 'monthly_excluded_symbols.csv',
    'monthly_excluded_symbols.txt', 'manifest.json',
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_nasdaq_listed(text: str) -> pd.DataFrame:
    from io import StringIO
    df = pd.read_csv(StringIO(text), sep="|")
    df = df[df["Symbol"].notna()]
    df = df[df["Symbol"] != "File Creation Time"]
    df["source"] = "nasdaq"
    df["name"] = df["Security Name"].fillna("")
    df["etf"] = df["ETF"].fillna("N").astype(str).str.upper().eq("Y")
    df["test_issue"] = df["Test Issue"].fillna("N").astype(str).str.upper().eq("Y")
    return df[["Symbol", "name", "etf", "test_issue", "source"]]


def parse_other_listed(text: str) -> pd.DataFrame:
    from io import StringIO
    df = pd.read_csv(StringIO(text), sep="|")
    df = df[df["ACT Symbol"].notna()]
    df = df[df["ACT Symbol"] != "File Creation Time"]
    df["source"] = "other"
    df["name"] = df["Security Name"].fillna("")
    df["etf"] = df["ETF"].fillna("N").astype(str).str.upper().eq("Y")
    df["test_issue"] = df["Test Issue"].fillna("N").astype(str).str.upper().eq("Y")
    return df.rename(columns={"ACT Symbol": "Symbol"})[["Symbol", "name", "etf", "test_issue", "source"]]


def build_universe_frame(nasdaq_text: str, other_text: str) -> pd.DataFrame:
    nasdaq = parse_nasdaq_listed(nasdaq_text)
    other = parse_other_listed(other_text)
    uni = pd.concat([nasdaq, other], ignore_index=True)
    uni = uni.drop_duplicates(subset=['Symbol']).reset_index(drop=True)
    uni['Symbol'] = uni['Symbol'].map(lambda x: str(x).upper())
    uni['yahoo_symbol'] = uni['Symbol'].map(lambda x: x.replace('.', '-'))
    return uni[['Symbol', 'yahoo_symbol', 'name', 'etf', 'test_issue', 'source']]


def download_bars_yfinance(symbols, period, stderr_path, batch=80, phase="DOWNLOAD"):
    """Fallback: Download daily bars using yfinance directly"""
    frames = {}
    misses = set()
    
    for i in range(0, len(symbols), batch):
        batch_symbols = symbols[i:i+batch]
        try:
            data = yf.download(
                tickers=batch_symbols, period=period, interval="1d",
                auto_adjust=False, group_by="ticker", progress=False,
                threads=False, prepost=False, timeout=30
            )
            if data is not None and len(data) > 0:
                if isinstance(data.columns, pd.MultiIndex):
                    for sym in batch_symbols:
                        if sym in data.columns.get_level_values(0):
                            sym_data = data[sym].dropna(subset=['Close', 'Volume'])
                            if len(sym_data) > 0:
                                frames[sym] = sym_data.reset_index()
                            else:
                                misses.add(sym)
                        else:
                            misses.add(sym)
                else:
                    sym = batch_symbols[0]
                    sym_data = data.dropna(subset=['Close', 'Volume'])
                    if len(sym_data) > 0:
                        frames[sym] = sym_data.reset_index()
                    else:
                        misses.add(sym)
            else:
                misses.update(batch_symbols)
        except Exception as e:
            with open(stderr_path, 'a') as f:
                f.write(f"{phase} error for {batch_symbols}: {e}\n")
            misses.update(batch_symbols)
    return frames, misses


def download_bars_curl_cffi(symbols, period, stderr_path, batch=80, phase="DOWNLOAD"):
    """Download using curl_cffi with Chrome fingerprint"""
    frames = {}
    misses = set()
    
    for i in range(0, len(symbols), batch):
        batch_symbols = symbols[i:i+batch]
        try:
            bars_dict, batch_misses = download_daily_bars(
                batch_symbols, period=period, stderr_path=stderr_path, 
                batch=batch, phase=phase
            )
            frames.update(bars_dict)
            misses.update(batch_misses)
        except Exception as e:
            with open(stderr_path, 'a') as f:
                f.write(f"{phase} curl_cffi error for {batch_symbols}: {e}\n")
            misses.update(batch_symbols)
    return frames, misses


def trailing_avg_dollar_volume(df, idx, days=30):
    if idx < 0 or len(df) == 0:
        return None
    start = max(0, idx - days + 1)
    window = df.iloc[start:idx+1]
    if 'Close' not in window.columns or 'Volume' not in window.columns:
        return None
    dv = (window['Close'] * window['Volume']).mean()
    return float(dv) if not pd.isna(dv) else None


def build_exclusion_rows(df: pd.DataFrame, *, stderr_path: str, period: str, batch: int, phase: str):
    # 硬編碼使用 'Symbol' 列
    symbols = df['Symbol'].astype(str).tolist()
    mapped = {s.replace('.', '-'): s for s in symbols}
    yahoo_symbols = list(mapped.keys())
    
    # 優先使用 curl_cffi，失敗回退到 yfinance
    if USE_CURL_CFFI:
        bars, misses = download_bars_curl_cffi(yahoo_symbols, period, stderr_path, batch=batch, phase=phase)
    else:
        bars, misses = download_bars_yfinance(yahoo_symbols, period, stderr_path, batch=batch, phase=phase)
    
    meta_by_symbol = {str(row['Symbol']): row for row in df.to_dict('records')}
    rows = []
    smallcap_symbols = []
    missing_symbols = []

    for yahoo_sym in sorted(misses):
        symbol = mapped.get(yahoo_sym, yahoo_sym)
        meta = meta_by_symbol.get(symbol, {})
        missing_symbols.append(symbol)
        rows.append({
            'symbol': symbol,
            'yahoo_symbol': yahoo_sym,
            'name': meta.get('name', ''),
            'reason': 'download_miss_or_possibly_delisted_or_not_yet_listed',
            'avg_dollar_volume_30d_usd': None,
            'valid_days': 0,
        })

    for yahoo_sym, price_df in bars.items():
        symbol = mapped.get(yahoo_sym, yahoo_sym)
        meta = meta_by_symbol.get(symbol, {})
        x = price_df.dropna(subset=['Close', 'Volume']).reset_index(drop=True)
        if len(x) == 0:
            missing_symbols.append(symbol)
            rows.append({
                'symbol': symbol,
                'yahoo_symbol': yahoo_sym,
                'name': meta.get('name', ''),
                'reason': 'empty_bars_or_possibly_delisted_or_not_yet_listed',
                'avg_dollar_volume_30d_usd': None,
                'valid_days': 0,
            })
            continue
        avg_dv = trailing_avg_dollar_volume(x, len(x) - 1, days=30)
        if avg_dv is not None and avg_dv < MIN_AVG_DOLLAR_VOL_30D:
            smallcap_symbols.append(symbol)
            rows.append({
                'symbol': symbol,
                'yahoo_symbol': yahoo_sym,
                'name': meta.get('name', ''),
                'reason': 'avg_dollar_volume_30d_below_15m_usd',
                'avg_dollar_volume_30d_usd': round(float(avg_dv), 2),
                'valid_days': int(len(x)),
            })

    generated_symbols = sorted(set(smallcap_symbols) | set(missing_symbols))
    rows.sort(key=lambda row: (row['reason'], row['symbol']))
    return rows, generated_symbols, sorted(set(smallcap_symbols)), sorted(set(missing_symbols))


def workspace_dir_from_arg(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    return ROOT / '.tmp' / 'universe_update'


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def write_shard_frames(df: pd.DataFrame, workspace_dir: Path, shard_count: int) -> list[dict]:
    shard_count = max(1, int(shard_count))
    symbols = df['Symbol'].astype(str).tolist()
    shard_symbol_lists = split_into_shards(symbols, shard_count)
    by_symbol = {str(row['Symbol']): row for row in df.to_dict('records')}
    shards_dir = workspace_dir / 'shards'
    shards_dir.mkdir(parents=True, exist_ok=True)
    shards_meta = []
    for idx, shard_symbols in enumerate(shard_symbol_lists, start=1):
        rows = [by_symbol[sym] for sym in shard_symbols if sym in by_symbol]
        cols = ['Symbol', 'yahoo_symbol', 'name', 'etf', 'test_issue', 'source']
        shard_df = pd.DataFrame(rows, columns=cols)
        shard_path = shards_dir / f'shard_{idx:02d}.csv'
        shard_df.to_csv(shard_path, index=False, encoding='utf-8')
        shards_meta.append({
            'shard_index': idx,
            'symbol_count': int(len(shard_df)),
            'path': str(shard_path.relative_to(workspace_dir)),
        })
    return shards_meta


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


def run_prepare(args) -> dict:
    root = ROOT
    out_dir = root / 'data' / 'universe'
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = workspace_dir_from_arg(args.workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    ensure_manual_exclusion_file(root)
    
    if not args.force_refresh:
        fresh, reason = cache_is_fresh(out_dir, skip_if_fresh_days=args.skip_if_fresh_days)
        if fresh:
            payload = {
                'status': 'skipped_fresh_cache',
                'reason': reason,
                'skip_if_fresh_days': args.skip_if_fresh_days,
                'force_refresh': args.force_refresh,
                'out_dir': str(out_dir),
                'workspace_dir': str(workspace_dir),
                'matrix': [],
            }
            write_json(workspace_dir / 'prepare.json', payload)
            return payload

    source_dir = workspace_dir / 'source'
    source_dir.mkdir(parents=True, exist_ok=True)
    
    # Download all universe files from shared repository
    shared_files = [
        'nasdaqlisted.txt',
        'otherlisted.txt',
        'us_symbols.csv',
        'monthly_excluded_symbols.json',
        'monthly_excluded_symbols.csv',
        'monthly_excluded_symbols.txt',
        'manifest.json',
        'yahoo_bad_symbols.txt',
    ]
    
    for fname in shared_files:
        url = f"{SHARED_UNIVERSE_BASE}/{fname}"
        try:
            text = fetch_text(url)
            (source_dir / fname).write_text(text, encoding='utf-8')
        except Exception as e:
            raise RuntimeError(f"Failed to download {fname} from shared repo: {e}")
    
    # Load us_symbols.csv to get symbols for sharding
    df = pd.read_csv(source_dir / 'us_symbols.csv')
    if args.max_symbols and args.max_symbols > 0:
        df = df.head(args.max_symbols).copy()

    shards_meta = write_shard_frames(df, workspace_dir, args.shard_count)
    payload = {
        'status': 'prepared',
        'generated_at_utc': now_utc().isoformat(),
        'period': args.period,
        'batch': int(args.batch),
        'shard_count': int(args.shard_count),
        'symbols': int(len(df)),
        'skip_if_fresh_days': args.skip_if_fresh_days,
        'force_refresh': args.force_refresh,
        'max_symbols': int(args.max_symbols),
        'workspace_dir': str(workspace_dir),
        'source_files': {
            'nasdaqlisted.txt': str((source_dir / 'nasdaqlisted.txt').relative_to(workspace_dir)),
            'otherlisted.txt': str((source_dir / 'otherlisted.txt').relative_to(workspace_dir)),
            'us_symbols.csv': str((source_dir / 'us_symbols.csv').relative_to(workspace_dir)),
        },
        'shards': shards_meta,
        'matrix': [{'shard_index': item['shard_index']} for item in shards_meta],
    }
    write_json(workspace_dir / 'prepare.json', payload)
    return payload


def cache_is_fresh(out_dir: Path, *, skip_if_fresh_days: float) -> tuple[bool, str]:
    if skip_if_fresh_days <= 0:
        return False, 'skip disabled'
    manifest_path = out_dir / 'manifest.json'
    if not manifest_path.exists():
        return False, 'manifest missing'
    missing = [name for name in REQUIRED_CACHE_FILES if not (out_dir / name).exists()]
    if missing:
        return False, f'missing required cache files: {", ".join(missing)}'
    try:
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
        updated = payload.get('updated_at_utc', '')
        updated_dt = datetime.fromisoformat(str(updated).replace('Z', '+00:00'))
    except Exception as e:
        return False, f'invalid manifest timestamp: {e}'
    age = now_utc() - updated_dt.astimezone(timezone.utc)
    threshold = timedelta(days=skip_if_fresh_days)
    if age <= threshold:
        return True, f'cache age {age} <= {threshold}'
    return False, f'cache age {age} > {threshold}'


def ensure_manual_exclusion_file(root: Path) -> Path:
    config_dir = root / 'config'
    config_dir.mkdir(parents=True, exist_ok=True)
    manual_exclude_path = config_dir / MANUAL_EXCLUSION_FILENAME
    if not manual_exclude_path.exists():
        manual_exclude_path.write_text('# One symbol per line, e.g. AAPL or BRK.B\n', encoding='utf-8')
    return manual_exclude_path


def run_shard(args) -> dict:
    workspace_dir = workspace_dir_from_arg(args.workspace_dir)
    prepare_payload = json.loads((workspace_dir / 'prepare.json').read_text(encoding='utf-8'))
    shard_index = int(args.shard_index)
    shard_path = workspace_dir / 'shards' / f'shard_{shard_index:02d}.csv'
    if not shard_path.exists():
        raise FileNotFoundError(f'shard file not found: {shard_path}')
    df = pd.read_csv(shard_path)
    
    results_dir = workspace_dir / 'results'
    results_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = results_dir / f'shard_{shard_index:02d}.stderr.log'
    if stderr_path.exists():
        stderr_path.unlink()

    rows, generated_symbols, smallcap_symbols, missing_symbols = build_exclusion_rows(
        df,
        stderr_path=str(stderr_path),
        period=args.period or prepare_payload['period'],
        batch=int(args.batch or prepare_payload['batch']),
        phase=f'MONTHLY_EXCLUSION_SHARD_{shard_index:02d}',
    )
    payload = {
        'status': 'completed',
        'generated_at_utc': now_utc().isoformat(),
        'shard_index': shard_index,
        'symbols': int(len(df)),
        'period': args.period or prepare_payload['period'],
        'batch': int(args.batch or prepare_payload['batch']),
        'generated_symbols': generated_symbols,
        'smallcap_symbols': smallcap_symbols,
        'missing_symbols': missing_symbols,
        'rows': rows,
        'stderr_file': str(stderr_path.relative_to(workspace_dir)),
    }
    write_json(results_dir / f'shard_{shard_index:02d}.json', payload)
    pd.DataFrame(rows).to_csv(results_dir / f'shard_{shard_index:02d}.csv', index=False, encoding='utf-8')
    return payload


def run_aggregate(args) -> dict:
    root = ROOT
    out_dir = root / 'data' / 'universe'
    out_dir.mkdir(parents=True, exist_ok=True)
    manual_exclude_path = ensure_manual_exclusion_file(root)
    workspace_dir = workspace_dir_from_arg(args.workspace_dir)
    prepare_payload = json.loads((workspace_dir / 'prepare.json').read_text(encoding='utf-8'))
    results_dir = workspace_dir / 'results'
    shard_files = sorted(results_dir.glob('shard_*.json'))
    expected = int(prepare_payload.get('shard_count', 0))
    if expected and len(shard_files) != expected:
        raise RuntimeError(f'expected {expected} shard results, found {len(shard_files)}')

    rows = []
    generated_symbols = set()
    smallcap_symbols = set()
    missing_symbols = set()
    shards_summary = []
    for shard_file in shard_files:
        payload = json.loads(shard_file.read_text(encoding='utf-8'))
        rows.extend(payload.get('rows', []))
        generated_symbols.update(payload.get('generated_symbols', []) or [])
        smallcap_symbols.update(payload.get('smallcap_symbols', []) or [])
        missing_symbols.update(payload.get('missing_symbols', []) or [])
        shards_summary.append({
            'shard_index': int(payload.get('shard_index', 0)),
            'symbols': int(payload.get('symbols', 0)),
            'generated_symbols': int(len(payload.get('generated_symbols', []) or [])),
            'missing_symbols': int(len(payload.get('missing_symbols', []) or [])),
        })

    rows.sort(key=lambda row: (row.get('reason', ''), row.get('symbol', '')))
    generated_symbols_sorted = sorted(generated_symbols)
    smallcap_symbols_sorted = sorted(smallcap_symbols)
    missing_symbols_sorted = sorted(missing_symbols)

    source_dir = workspace_dir / 'source'
    nasdaq_text = (source_dir / 'nasdaqlisted.txt').read_text(encoding='utf-8')
    other_text = (source_dir / 'otherlisted.txt').read_text(encoding='utf-8')
    us_symbols_csv_workspace = source_dir / 'us_symbols.csv'

    (out_dir / 'nasdaqlisted.txt').write_text(nasdaq_text, encoding='utf-8')
    (out_dir / 'otherlisted.txt').write_text(other_text, encoding='utf-8')
    us_symbols_csv = out_dir / 'us_symbols.csv'
    us_symbols_csv.write_bytes(us_symbols_csv_workspace.read_bytes())

    exclusion_payload = {
        'updated_at_utc': now_utc().isoformat(),
        'thresholds': {
            'smallcap_avg_dollar_volume_30d_usd': MIN_AVG_DOLLAR_VOL_30D,
            'market_data_period': args.period or prepare_payload['period'],
            'market_data_batch': int(args.batch or prepare_payload['batch']),
        },
        'counts': {
            'symbols': int(pd.read_csv(us_symbols_csv).shape[0]),
            'smallcap_symbols': int(len(smallcap_symbols_sorted)),
            'missing_symbols': int(len(missing_symbols_sorted)),
            'generated_symbols': int(len(generated_symbols_sorted)),
            'manual_exclusions': int(len(load_manual_exclusions(manual_exclude_path))),
        },
        'generated_symbols': generated_symbols_sorted,
        'smallcap_symbols': smallcap_symbols_sorted,
        'missing_symbols': missing_symbols_sorted,
        'rows': rows,
    }
    exclusion_json_path = out_dir / MONTHLY_EXCLUSION_FILENAME
    exclusion_json_path.write_text(json.dumps(exclusion_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    exclusion_csv_path = out_dir / 'monthly_excluded_symbols.csv'
    pd.DataFrame(rows).to_csv(exclusion_csv_path, index=False, encoding='utf-8')
    exclusion_txt_path = out_dir / 'monthly_excluded_symbols.txt'
    exclusion_txt_path.write_text('\n'.join(generated_symbols_sorted) + ('\n' if generated_symbols_sorted else ''), encoding='utf-8')

    return {'status': 'aggregated', 'exclusion_path': str(exclusion_json_path)}


def load_manual_exclusions(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip().upper()
        if not line or line.startswith('#'):
            continue
        out.add(line)
        out.add(line.replace('-', '.'))
        out.add(line.replace('.', '-'))
    return out


def main():
    parser = argparse.ArgumentParser(description='Monthly US Symbol Universe Updater')
    parser.add_argument('--mode', choices=['prepare', 'shard', 'aggregate'], required=True)
    parser.add_argument('--workspace-dir', type=str, default=None)
    parser.add_argument('--period', type=str, default=DEFAULT_PERIOD)
    parser.add_argument('--batch', type=int, default=DEFAULT_BATCH)
    parser.add_argument('--shard-count', type=int, default=DEFAULT_SHARD_COUNT)
    parser.add_argument('--shard-index', type=int, default=1)
    parser.add_argument('--skip-if-fresh-days', type=float, default=25.0)
    parser.add_argument('--force-refresh', action='store_true')
    parser.add_argument('--max-symbols', type=int, default=0)
    args = parser.parse_args()

    if args.mode == 'prepare':
        payload = run_prepare(args)
    elif args.mode == 'shard':
        payload = run_shard(args)
    elif args.mode == 'aggregate':
        payload = run_aggregate(args)
    else:
        raise ValueError(f'Unknown mode: {args.mode}')
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
