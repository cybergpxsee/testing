#!/usr/bin/env python3
from pathlib import Path
from urllib.request import Request, urlopen
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import us_pattern_scan as base

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
UA = "Mozilla/5.0 (X11; Linux x86_64) pullback-scan-github-action/1.0"
SMALLCAP_AVG_DOLLAR_VOLUME_30D_USD = 15_000_000
DEFAULT_SCAN_PERIOD = '2mo'
DEFAULT_BATCH = 80
DEFAULT_SHARD_COUNT = 4
DEFAULT_WORK_DIRNAME = '.tmp/universe_update'
MANUAL_EXCLUSION_FILENAME = 'exclude_symbols.txt'
MONTHLY_EXCLUSION_FILENAME = 'monthly_excluded_symbols.json'


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    last_error: Exception = RuntimeError(f'failed to fetch url: {url}')
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt >= 3:
                break
            time.sleep(2 * attempt)
    raise last_error


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_dir(root: Path) -> Path:
    return root / 'config'


def default_output_dir(root: Path) -> Path:
    return root / 'data' / 'universe'


def default_work_dir(root: Path) -> Path:
    return root / DEFAULT_WORK_DIRNAME


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


def build_universe_frame(nasdaq_text: str, other_text: str) -> pd.DataFrame:
    nasdaq = base.parse_nasdaq_listed(nasdaq_text)
    other = base.parse_other_listed(other_text)
    uni = pd.concat([nasdaq, other], ignore_index=True)
    uni = uni.drop_duplicates(subset=['Symbol']).reset_index(drop=True)
    uni['keep'] = uni.apply(lambda r: base.is_regular_security(r['Symbol'], r['name'], bool(r['etf']), bool(r['test_issue'])), axis=1)
    uni = uni[uni['keep']].copy().reset_index(drop=True)
    uni['yahoo_symbol'] = uni['Symbol'].astype(str).map(base.yahoo_symbol)
    return uni[['Symbol', 'yahoo_symbol', 'name', 'etf', 'test_issue', 'source']]


def split_dataframe(df: pd.DataFrame, shard_count: int) -> list[pd.DataFrame]:
    shard_count = max(1, int(shard_count))
    if len(df) == 0:
        return [df.iloc[0:0].copy() for _ in range(shard_count)]
    shard_size = math.ceil(len(df) / shard_count)
    shards: list[pd.DataFrame] = []
    for index in range(shard_count):
        start = index * shard_size
        end = min(len(df), start + shard_size)
        shard_df = df.iloc[start:end].copy() if start < len(df) else df.iloc[0:0].copy()
        shards.append(shard_df)
    return shards


def load_prepare_payload(work_dir: Path) -> dict:
    prepare_path = work_dir / 'prepare.json'
    if not prepare_path.exists():
        raise RuntimeError(f'missing prepare metadata: {prepare_path}')
    return json.loads(prepare_path.read_text(encoding='utf-8'))


def shard_input_path(work_dir: Path, shard_index: int) -> Path:
    return work_dir / 'shards' / f'shard_{shard_index:02d}.csv'


def shard_output_json_path(work_dir: Path, shard_index: int) -> Path:
    return work_dir / 'shard-results' / f'shard_{shard_index:02d}.json'


def build_exclusion_rows_for_shard(df: pd.DataFrame, *, stderr_path: str, period: str, batch: int):
    symbols = df['Symbol'].astype(str).tolist()
    mapped = {base.yahoo_symbol(sym): sym for sym in symbols}
    yahoo_symbols = list(mapped.keys())
    bars, misses = base.download_bars(yahoo_symbols, period, stderr_path, batch=batch, phase='MONTHLY_EXCLUSION')
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
        avg_dollar_volume_30d = base.trailing_avg_dollar_volume(x, len(x) - 1, days=30)
        if avg_dollar_volume_30d is not None and avg_dollar_volume_30d < SMALLCAP_AVG_DOLLAR_VOLUME_30D_USD:
            smallcap_symbols.append(symbol)
            rows.append({
                'symbol': symbol,
                'yahoo_symbol': yahoo_sym,
                'name': meta.get('name', ''),
                'reason': 'avg_dollar_volume_30d_below_15m_usd',
                'avg_dollar_volume_30d_usd': round(float(avg_dollar_volume_30d), 2),
                'valid_days': int(len(x)),
            })

    generated_symbols = sorted(set(smallcap_symbols) | set(missing_symbols))
    rows.sort(key=lambda row: (row['reason'], row['symbol']))
    return rows, generated_symbols, sorted(set(smallcap_symbols)), sorted(set(missing_symbols))


def prepare_shards(*, output_dir: Path, work_dir: Path, shard_count: int, max_symbols: int) -> dict:
    output_dir = ensure_dir(output_dir)
    work_dir = ensure_dir(work_dir)
    ensure_dir(work_dir / 'shards')
    ensure_dir(work_dir / 'shard-results')

    nasdaq_text = fetch_text(NASDAQ_LISTED_URL)
    other_text = fetch_text(OTHER_LISTED_URL)
    (output_dir / 'nasdaqlisted.txt').write_text(nasdaq_text, encoding='utf-8')
    (output_dir / 'otherlisted.txt').write_text(other_text, encoding='utf-8')

    df = build_universe_frame(nasdaq_text, other_text)
    if max_symbols and max_symbols > 0:
        df = df.head(max_symbols).copy()

    source_csv = work_dir / 'us_symbols.source.csv'
    df.to_csv(source_csv, index=False, encoding='utf-8')

    shard_frames = split_dataframe(df, shard_count)
    matrix = {'include': []}
    shard_summaries = []
    for idx, shard_df in enumerate(shard_frames, start=1):
        path = shard_input_path(work_dir, idx)
        shard_df.to_csv(path, index=False, encoding='utf-8')
        summary = {
            'shard_index': idx,
            'path': str(path.relative_to(work_dir)),
            'rows': int(len(shard_df)),
            'symbols': shard_df['Symbol'].astype(str).tolist() if 'Symbol' in shard_df.columns else [],
        }
        shard_summaries.append(summary)
        matrix['include'].append({
            'shard_index': idx,
            'shard_file': summary['path'],
        })

    payload = {
        'prepared_at_utc': utc_now_iso(),
        'shard_count': int(shard_count),
        'max_symbols': int(max_symbols or 0),
        'counts': {
            'symbols': int(len(df)),
        },
        'sources': {
            'nasdaqlisted': NASDAQ_LISTED_URL,
            'otherlisted': OTHER_LISTED_URL,
        },
        'files': {
            'nasdaqlisted.txt': {
                'sha256': sha256_bytes(nasdaq_text.encode('utf-8')),
                'bytes': len(nasdaq_text.encode('utf-8')),
            },
            'otherlisted.txt': {
                'sha256': sha256_bytes(other_text.encode('utf-8')),
                'bytes': len(other_text.encode('utf-8')),
            },
            'us_symbols.source.csv': {
                'sha256': sha256_bytes(source_csv.read_bytes()),
                'bytes': source_csv.stat().st_size,
            },
        },
        'shards': shard_summaries,
        'matrix': matrix,
    }
    (work_dir / 'prepare.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    (work_dir / 'matrix.json').write_text(json.dumps(matrix, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    return payload


def run_single_shard(*, work_dir: Path, shard_index: int, period: str, batch: int, stderr_path: str | None = None) -> dict:
    prepare_payload = load_prepare_payload(work_dir)
    shard_count = int(prepare_payload['shard_count'])
    if shard_index < 1 or shard_index > shard_count:
        raise RuntimeError(f'invalid shard index/count: {shard_index}/{shard_count}')

    input_path = shard_input_path(work_dir, shard_index)
    if not input_path.exists():
        raise RuntimeError(f'missing shard input: {input_path}')

    df = pd.read_csv(input_path, dtype={'Symbol': str}).fillna('')
    if len(df) > 0:
        df = df.sort_values(['Symbol', 'yahoo_symbol'], kind='stable').reset_index(drop=True)
    stderr_path = stderr_path or str(work_dir / 'monthly_excluded_symbols.stderr.log')
    rows, generated_symbols, smallcap_symbols, missing_symbols = build_exclusion_rows_for_shard(
        df,
        stderr_path=stderr_path,
        period=period,
        batch=batch,
    )

    summary = {
        'shard_index': shard_index,
        'processed_at_utc': utc_now_iso(),
        'input_rows': int(len(df)),
        'counts': {
            'generated_symbols': int(len(generated_symbols)),
            'smallcap_symbols': int(len(smallcap_symbols)),
            'missing_symbols': int(len(missing_symbols)),
        },
        'generated_symbols': generated_symbols,
        'smallcap_symbols': smallcap_symbols,
        'missing_symbols': missing_symbols,
        'rows': rows,
    }
    out_path = shard_output_json_path(work_dir, shard_index)
    ensure_dir(out_path.parent)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def aggregate_shards(*, root: Path, output_dir: Path, work_dir: Path, shard_count: int, period: str, batch: int) -> dict:
    output_dir = ensure_dir(output_dir)
    prepare_payload = load_prepare_payload(work_dir)
    expected_shards = int(prepare_payload['shard_count'])
    if int(shard_count) != expected_shards:
        raise RuntimeError(f'shard count mismatch: aggregate={shard_count}, prepared={expected_shards}')

    all_rows = []
    generated_symbols = set()
    smallcap_symbols = set()
    missing_symbols = set()
    shard_summaries = []
    for idx in range(1, expected_shards + 1):
        path = shard_output_json_path(work_dir, idx)
        if not path.exists():
            raise RuntimeError(f'missing shard output for shard {idx}: {path}')
        payload = json.loads(path.read_text(encoding='utf-8'))
        shard_summaries.append({
            'shard_index': idx,
            'processed_at_utc': payload.get('processed_at_utc'),
            'input_rows': int(payload.get('input_rows') or 0),
            'generated_symbols': int((payload.get('counts') or {}).get('generated_symbols') or 0),
            'smallcap_symbols': int((payload.get('counts') or {}).get('smallcap_symbols') or 0),
            'missing_symbols': int((payload.get('counts') or {}).get('missing_symbols') or 0),
        })
        all_rows.extend(payload.get('rows') or [])
        generated_symbols.update(payload.get('generated_symbols') or [])
        smallcap_symbols.update(payload.get('smallcap_symbols') or [])
        missing_symbols.update(payload.get('missing_symbols') or [])

    all_rows.sort(key=lambda row: (row.get('reason', ''), row.get('symbol', '')))
    final_df = pd.read_csv(work_dir / 'us_symbols.source.csv', dtype={'Symbol': str}).fillna('')
    if len(final_df) > 0:
        final_df = final_df.drop_duplicates(subset=['Symbol']).sort_values(['Symbol'], kind='stable').reset_index(drop=True)
    us_symbols_csv = output_dir / 'us_symbols.csv'
    final_df.to_csv(us_symbols_csv, index=False, encoding='utf-8')

    exclusion_payload = {
        'updated_at_utc': utc_now_iso(),
        'thresholds': {
            'smallcap_avg_dollar_volume_30d_usd': SMALLCAP_AVG_DOLLAR_VOLUME_30D_USD,
            'market_data_period': period,
            'market_data_batch': batch,
        },
        'counts': {
            'symbols': int(len(final_df)),
            'smallcap_symbols': int(len(smallcap_symbols)),
            'missing_symbols': int(len(missing_symbols)),
            'generated_symbols': int(len(generated_symbols)),
            'manual_exclusions': int(len(load_manual_exclusions(default_config_dir(root) / MANUAL_EXCLUSION_FILENAME))),
        },
        'generated_symbols': sorted(generated_symbols),
        'smallcap_symbols': sorted(smallcap_symbols),
        'missing_symbols': sorted(missing_symbols),
        'rows': all_rows,
    }

    exclusion_json_path = output_dir / MONTHLY_EXCLUSION_FILENAME
    exclusion_json_path.write_text(json.dumps(exclusion_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    exclusion_csv_path = output_dir / 'monthly_excluded_symbols.csv'
    pd.DataFrame(all_rows).to_csv(exclusion_csv_path, index=False, encoding='utf-8')
    exclusion_txt_path = output_dir / 'monthly_excluded_symbols.txt'
    exclusion_txt_path.write_text('\n'.join(sorted(generated_symbols)) + ('\n' if generated_symbols else ''), encoding='utf-8')

    nasdaq_path = output_dir / 'nasdaqlisted.txt'
    other_path = output_dir / 'otherlisted.txt'
    if not nasdaq_path.exists() or not other_path.exists():
        raise RuntimeError('missing Nasdaq Trader source files in output dir after prepare phase')
    nasdaq_bytes = nasdaq_path.read_bytes()
    other_bytes = other_path.read_bytes()

    manifest = {
        'updated_at_utc': utc_now_iso(),
        'sources': {
            'nasdaqlisted': NASDAQ_LISTED_URL,
            'otherlisted': OTHER_LISTED_URL,
            'yahoo_finance_daily_bars': 'yfinance',
        },
        'rules': {
            'universe_filter': 'Nasdaq Trader regular securities + Yahoo-friendly filter',
            'pre_scan_generated_exclusions': '30日平均成交額 < 1500萬美元，或 Yahoo 對不到 / 可能已退市 / 未上市代號',
        },
        'counts': {
            'symbols': int(len(final_df)),
            'generated_exclusions': int(len(generated_symbols)),
            'smallcap_symbols': int(len(smallcap_symbols)),
            'missing_symbols': int(len(missing_symbols)),
        },
        'updater': {
            'mode': 'matrix_prepare_shard_aggregate',
            'shard_count': expected_shards,
            'prepared_at_utc': prepare_payload.get('prepared_at_utc'),
            'aggregated_at_utc': utc_now_iso(),
            'max_symbols': int(prepare_payload.get('max_symbols') or 0),
            'market_data_period': period,
            'market_data_batch': batch,
            'work_dir': str(work_dir),
        },
        'shards': shard_summaries,
        'files': {
            'nasdaqlisted.txt': {
                'sha256': sha256_bytes(nasdaq_bytes),
                'bytes': len(nasdaq_bytes),
            },
            'otherlisted.txt': {
                'sha256': sha256_bytes(other_bytes),
                'bytes': len(other_bytes),
            },
            'us_symbols.csv': {
                'sha256': sha256_bytes(us_symbols_csv.read_bytes()),
                'bytes': us_symbols_csv.stat().st_size,
            },
            'monthly_excluded_symbols.json': {
                'sha256': sha256_bytes(exclusion_json_path.read_bytes()),
                'bytes': exclusion_json_path.stat().st_size,
            },
            'monthly_excluded_symbols.csv': {
                'sha256': sha256_bytes(exclusion_csv_path.read_bytes()),
                'bytes': exclusion_csv_path.stat().st_size,
            },
            'monthly_excluded_symbols.txt': {
                'sha256': sha256_bytes(exclusion_txt_path.read_bytes()),
                'bytes': exclusion_txt_path.stat().st_size,
            },
        },
    }
    manifest_path = output_dir / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {
        'manifest': manifest,
        'manual_exclude_path': str(default_config_dir(root) / MANUAL_EXCLUSION_FILENAME),
        'work_dir': str(work_dir),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def run_full(*, root: Path, output_dir: Path, work_dir: Path, shard_count: int, max_symbols: int, period: str, batch: int, stderr_path: str) -> dict:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    prepare_payload = prepare_shards(output_dir=output_dir, work_dir=work_dir, shard_count=shard_count, max_symbols=max_symbols)
    indices = [entry['shard_index'] for entry in prepare_payload['shards']]
    procs: list[tuple[int, subprocess.Popen, Path, Path]] = []
    for idx in indices:
        stdout_file = work_dir / 'shard-results' / f'shard_{idx:02d}.stdout.log'
        stderr_file = work_dir / 'shard-results' / f'shard_{idx:02d}.stderr.log'
        cmd = [
            os.environ.get('PYTHON_FOR_UPDATE_UNIVERSE') or sys.executable,
            str(Path(__file__).resolve()),
            '--mode', 'run-shard',
            '--shard-count', str(shard_count),
            '--shard-index', str(idx),
            '--period', period,
            '--batch', str(batch),
            '--output-dir', str(output_dir),
            '--work-dir', str(work_dir),
            '--stderr-path', stderr_path,
        ]
        with stdout_file.open('w', encoding='utf-8') as out_fh, stderr_file.open('w', encoding='utf-8') as err_fh:
            proc = subprocess.Popen(cmd, stdout=out_fh, stderr=err_fh, text=True)
        procs.append((idx, proc, stdout_file, stderr_file))

    failures = []
    for idx, proc, stdout_file, stderr_file in procs:
        proc.wait()
        if proc.returncode != 0:
            failures.append({
                'shard_index': idx,
                'returncode': proc.returncode,
                'stdout': stdout_file.read_text(encoding='utf-8', errors='replace') if stdout_file.exists() else '',
                'stderr': stderr_file.read_text(encoding='utf-8', errors='replace') if stderr_file.exists() else '',
            })
    if failures:
        raise RuntimeError(f'shard workers failed: {json.dumps(failures, ensure_ascii=False)[:4000]}')

    return aggregate_shards(root=root, output_dir=output_dir, work_dir=work_dir, shard_count=shard_count, period=period, batch=batch)


def main() -> None:
    parser = argparse.ArgumentParser(description='Update US universe cache and monthly exclusion list.')
    parser.add_argument('--mode', choices=['full', 'prepare-shards', 'run-shard', 'aggregate-shards'], default='full')
    parser.add_argument('--max-symbols', type=int, default=0, help='Optional cap for smoke tests.')
    parser.add_argument('--batch', type=int, default=DEFAULT_BATCH)
    parser.add_argument('--period', default=DEFAULT_SCAN_PERIOD)
    parser.add_argument('--stderr-path', default='')
    parser.add_argument('--shard-count', type=int, default=DEFAULT_SHARD_COUNT)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--output-dir', default=str(default_output_dir(ROOT)), help='Final cache output directory.')
    parser.add_argument('--work-dir', default=str(default_work_dir(ROOT)), help='Temporary shard work directory.')
    args = parser.parse_args()

    root = ROOT
    output_dir = Path(args.output_dir).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    out_dir = ensure_dir(output_dir)
    config_dir = default_config_dir(root)
    config_dir.mkdir(parents=True, exist_ok=True)
    manual_exclude_path = config_dir / MANUAL_EXCLUSION_FILENAME
    if not manual_exclude_path.exists():
        manual_exclude_path.write_text('# One symbol per line, e.g. AAPL or BRK.B\n', encoding='utf-8')
    stderr_path = args.stderr_path or str(out_dir / 'monthly_excluded_symbols.stderr.log')
    shard_count = max(1, int(args.shard_count or DEFAULT_SHARD_COUNT))

    if args.mode == 'prepare-shards':
        payload = prepare_shards(output_dir=output_dir, work_dir=work_dir, shard_count=shard_count, max_symbols=args.max_symbols)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.mode == 'run-shard':
        if args.shard_index < 1:
            raise RuntimeError('--shard-index must be >= 1 in run-shard mode')
        payload = run_single_shard(work_dir=work_dir, shard_index=args.shard_index, period=args.period, batch=args.batch, stderr_path=stderr_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.mode == 'aggregate-shards':
        aggregate_shards(root=root, output_dir=output_dir, work_dir=work_dir, shard_count=shard_count, period=args.period, batch=args.batch)
        return

    run_full(root=root, output_dir=output_dir, work_dir=work_dir, shard_count=shard_count, max_symbols=args.max_symbols, period=args.period, batch=args.batch, stderr_path=stderr_path)


if __name__ == '__main__':
    main()
