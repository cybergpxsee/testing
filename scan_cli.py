#!/usr/bin/env python3
"""
CLI entry point for US Pullback Scanner.
Replaces inline Python in run_scan.sh for better maintainability.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

import pandas as pd

# Import from the main scanner module
from us_pattern_scan import (
    parse_nasdaq_listed, parse_other_listed, fetch_text,
    NASDAQ_LISTED_URL, OTHER_LISTED_URL,
    is_regular_security, yahoo_symbol,
    download_bars, split_into_shards,
    scan_stage2_dataset,
    trailing_avg_dollar_volume, liquidity_band_from_avg_dollar_volume,
    log_info as append_log,
    make_result, clone_row_for_liquidity_band,
    render_markdown_report,
    aggregate_shard_results,
    get_exclusion_lists,  # 新增导入
)


def run_scan(
    format: str = 'json',
    max_symbols: int = 0,
    stderr_path: str = '/tmp/us_pattern_scan_yf_stderr.log',
    shards: int = 4,
    artifact_dir: str = '',
    stage1_period: str = '1mo',
    stage1_batch: int = 90,
    stage2_batch: int = 120,
    shard_index: int = 0,
    total_shards: int = 1,
    symbols: str = '',
) -> Union[str, dict]:
    """
    Run the full scan pipeline.
    
    Args:
        format: Output format ('json' or 'markdown')
        max_symbols: Optional cap on universe size
        stderr_path: Path to stderr log file
        shards: Number of stage2 shards
        artifact_dir: Optional directory for shard artifacts
        stage1_period: Short lookback for liquidity screening
        stage1_batch: Batch size for stage1 download
        stage2_batch: Batch size for stage2 download
        
    Returns:
        Output dictionary with scan results
    """
    # Setup paths
    stderr_path = Path(stderr_path).expanduser().resolve()
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    open(stderr_path, 'w').close()
    
    if artifact_dir:
        artifact_dir = Path(artifact_dir).expanduser()
    else:
        artifact_dir = stderr_path.parent / (stderr_path.stem + '.artifacts')
    artifact_dir.mkdir(parents=True, exist_ok=True)
    
    append_log(f"SCAN_START format={format} max_symbols={max_symbols or 'all'} "
        f"shards={max(1, shards)} stage1_period={stage1_period} "
        f"stage1_batch={stage1_batch} stage2_batch={stage2_batch}"
    )
    
    # Download universe (or use provided symbols)
    if symbols:
        # Matrix mode: symbols provided by prepare step - SKIP Stage 1 entirely
        yahoo_symbols = [s.strip().upper() for s in symbols.split(',') if s.strip()]
        original_symbols = [s.replace('-', '.') for s in yahoo_symbols]
        mapped = {ys: ys.replace('-', '.') for ys in yahoo_symbols}
        append_log(f"MATRIX_MODE: processing {len(yahoo_symbols)} symbols directly (skipping Stage 1)")
        # Override sharding parameters for matrix mode
        shards = 1
        total_shards = 1
        shard_index = 0
        miss1 = set()
        # All provided symbols are considered liquid (already filtered in prepare)
        liquid = yahoo_symbols
    else:
        # Normal mode: download and filter universe
        nasdaq = parse_nasdaq_listed(fetch_text(NASDAQ_LISTED_URL))
        other = parse_other_listed(fetch_text(OTHER_LISTED_URL))
        uni = pd.concat([nasdaq, other], ignore_index=True)
        uni = uni.drop_duplicates(subset=['Symbol']).reset_index(drop=True)
        uni['keep'] = uni.apply(
            lambda r: is_regular_security(r['Symbol'], r['name'], bool(r['etf']), bool(r['test_issue'])),
            axis=1
        )
        uni = uni[uni['keep']].copy()
        
        # ---- 使用远程排除列表（回退本地） ----
        manual_exclusions, monthly_exclusions = get_exclusion_lists()
        all_exclusions = manual_exclusions | monthly_exclusions
        pre_filter_count = len(uni)
        if all_exclusions:
            uni = uni[~uni['Symbol'].isin(all_exclusions)].copy()
            append_log(f"Excluded {pre_filter_count - len(uni)} symbols using manual+monthly exclusion lists (remote + fallback)")
        # ---------------------------------------
        
        if max_symbols and max_symbols > 0:
            uni = uni.head(max_symbols).copy()
        
        original_symbols = uni['Symbol'].tolist()
        mapped = {yahoo_symbol(sym): sym for sym in original_symbols}
        yahoo_symbols = list(mapped.keys())
        
        append_log(f"STAGE1_START universe={len(yahoo_symbols)}")
        
        # Stage 1: Liquidity screening
        stage1, miss1 = download_bars(
            yahoo_symbols, stage1_period, stderr_path,
            batch=stage1_batch, phase='STAGE1'
        )
        
        liquid = []
        for ys, df in stage1.items():
            x = df.dropna(subset=['Close', 'Volume']).reset_index(drop=True)
            if len(x) == 0:
                continue
            avg_dollar_vol_20d = trailing_avg_dollar_volume(x, len(x) - 1, days=20)
            if avg_dollar_vol_20d is not None and avg_dollar_vol_20d >= 20_000_000:
                liquid.append(ys)
        
        append_log(f"STAGE1_DONE ok={len(stage1)} liquid={len(liquid)} misses={len(miss1)}")
        
        # Save liquid symbols
        (artifact_dir / 'liquid_symbols.json').write_text(
            json.dumps({
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'universe_total': len(yahoo_symbols),
                'liquid_count': len(liquid),
                'liquid_symbols': liquid,
            }, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    
    # Stage 2: Deep scan
    # In matrix mode (symbols provided), all symbols are the job's workload - don't shard further
    if symbols:
        # Matrix mode: all liquid symbols belong to this worker, process as single shard
        shard_lists = [liquid]
        shards = 1
    else:
        shard_lists = split_into_shards(liquid, max(1, shards))
    
    results = []
    long_count = 0
    short_count = 0
    deep_scan_count = 0
    miss2 = set()
    shard_summaries = []
    
    # Determine which shards to process (for matrix parallel execution)
    if total_shards > 1 and shard_index >= 0:
        # Matrix mode: only process the assigned shard
        if shard_index < len(shard_lists):
            process_indices = [shard_index]
        else:
            process_indices = []
    else:
        # Sequential mode: process all shards
        process_indices = list(range(len(shard_lists)))
    
    for shard_idx in process_indices:
        shard_symbols = shard_lists[shard_idx]
        shard_num = shard_idx + 1
        append_log(f"STAGE2_SHARD_START shard={shard_num}/{len(shard_lists)} symbols={len(shard_symbols)}")
        stage2, shard_miss = download_bars(
            shard_symbols, '1y', stderr_path,
            batch=stage2_batch, phase=f'STAGE2_SHARD_{shard_num:02d}'
        )
        shard_results, shard_long, shard_short = scan_stage2_dataset(
            stage2, mapped, stderr_path
        )
        deep_scan_count += len(stage2)
        miss2.update(shard_miss)
        results.extend(shard_results)
        long_count += shard_long
        short_count += shard_short
        
        shard_summary = {
            'shard': shard_num,
            'input_symbols': len(shard_symbols),
            'downloaded_symbols': len(stage2),
            'misses': len(shard_miss),
            'candidates': len(shard_results),
            'long_candidates': shard_long,
            'short_candidates': shard_short,
        }
        shard_summaries.append(shard_summary)
        
        shard_path = artifact_dir / f'shard_{shard_num:02d}.json'
        shard_path.write_text(
            json.dumps({
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'summary': shard_summary,
                'results': shard_results,
                'miss_symbols': sorted(list(shard_miss)),
            }, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        
        append_log(f"STAGE2_SHARD_DONE shard={shard_num}/{len(shard_lists)} "
            f"downloaded={len(stage2)} misses={len(shard_miss)} candidates={len(shard_results)}"
        )
    
    # Sort and deduplicate
    results.sort(key=lambda x: (x['_sort_pullback'], x['score'], x['_sort_event'], x['_sort_confirm']), reverse=True)
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
    
    # Band rows
    band_rows_20m_to_50m = []
    band_rows_50m_plus = []
    for row in deduped:
        row_20m_to_50m = clone_row_for_liquidity_band(row, '20m_to_50m')
        row_50m_plus = clone_row_for_liquidity_band(row, '50m_plus')
        if row_20m_to_50m:
            band_rows_20m_to_50m.append(row_20m_to_50m)
        if row_50m_plus:
            band_rows_50m_plus.append(row_50m_plus)
    
    band_rows_20m_to_50m.sort(key=lambda x: (x['_sort_pullback'], x['score'], x['_sort_event'], x['_sort_confirm']), reverse=True)
    band_rows_50m_plus.sort(key=lambda x: (x['_sort_pullback'], x['score'], x['_sort_event'], x['_sort_confirm']), reverse=True)
    
    top10_long_20m_to_50m = [row for row in band_rows_20m_to_50m if row['direction'] == '做多'][:10]
    top10_short_20m_to_50m = [row for row in band_rows_20m_to_50m if row['direction'] == '做空'][:10]
    top10_long_50m_plus = [row for row in band_rows_50m_plus if row['direction'] == '做多'][:10]
    top10_short_50m_plus = [row for row in band_rows_50m_plus if row['direction'] == '做空'][:10]
    
    # Build output
    out = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'data_sources': [
            'Nasdaq Trader nasdaqlisted.txt',
            'Nasdaq Trader otherlisted.txt',
            'Yahoo Finance / yfinance 日线 OHLCV',
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
        'stderr_log': str(stderr_path),
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
    
    (artifact_dir / 'final_output.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8'
    )
    
    append_log(f"SCAN_DONE deep_scan={deep_scan_count} candidates={len(results)} deduped={len(deduped)}")
    
    if format == 'markdown':
        return render_markdown_report(out)
    else:
        return json.dumps(out, ensure_ascii=False, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(description='U.S. pullback pattern scan')
    parser.add_argument('--format', choices=['json', 'markdown'], default='json')
    parser.add_argument('--max-symbols', type=int, default=0, help='Optional cap on universe size for smoke tests')
    parser.add_argument('--stderr-path', default='/tmp/us_pattern_scan_yf_stderr.log')
    parser.add_argument('--shards', type=int, default=int(os.environ.get('HERMES_SCAN_SHARDS', '4')))
    parser.add_argument('--artifact-dir', default=os.environ.get('HERMES_SCAN_ARTIFACT_DIR', ''))
    parser.add_argument('--stage1-period', default=os.environ.get('HERMES_SCAN_STAGE1_PERIOD', '1mo'))
    parser.add_argument('--stage1-batch', type=int, default=int(os.environ.get('HERMES_SCAN_STAGE1_BATCH', '90')))
    parser.add_argument('--stage2-batch', type=int, default=int(os.environ.get('HERMES_SCAN_STAGE2_BATCH', '120')))
    parser.add_argument('--shard-index', type=int, default=int(os.environ.get('HERMES_SCAN_SHARD_INDEX', '0')), help='Shard index (0-based) for matrix parallel scan')
    parser.add_argument('--total-shards', type=int, default=int(os.environ.get('HERMES_SCAN_TOTAL_SHARDS', '1')), help='Total number of shards for matrix parallel scan')
    parser.add_argument('--symbols', type=str, default='', help='Comma-separated list of Yahoo symbols to scan (bypasses universe download)')
    parser.add_argument('--aggregate', type=str, default='', help='Path to directory with shard artifacts to aggregate')
    parser.add_argument('--output', type=str, default='', help='Output directory for aggregated results')
    args = parser.parse_args()
    
    if args.aggregate:
        # Aggregate mode: merge shard artifacts
        from us_pattern_scan import aggregate_shard_results
        result = aggregate_shard_results(args.aggregate, args.output)
        print(result)
        return
    
    result = run_scan(
        format=args.format,
        max_symbols=args.max_symbols,
        stderr_path=args.stderr_path,
        shards=args.shards,
        artifact_dir=args.artifact_dir,
        stage1_period=args.stage1_period,
        stage1_batch=args.stage1_batch,
        stage2_batch=args.stage2_batch,
        shard_index=args.shard_index,
        total_shards=args.total_shards,
        symbols=args.symbols,
    )
    
    print(result)


if __name__ == '__main__':
    main()
