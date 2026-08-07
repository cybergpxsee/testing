#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/output}"
PYTHON_BIN="${HERMES_SCAN_PYTHON:-python}"
mkdir -p "$OUT_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$OUT_DIR/$STAMP"
ARTIFACT_ROOT="$RUN_DIR/artifacts"
JSON_PATH="$RUN_DIR/consolidation_scan.json"
MD_PATH="$RUN_DIR/consolidation_scan.md"
COMBINED_STAGE1="$RUN_DIR/liquid_symbols.json"
FINAL_DIR="$RUN_DIR/final"
COMBINED_STDERR="$RUN_DIR/consolidation_scan.stderr.log"
mkdir -p "$ARTIFACT_ROOT" "$FINAL_DIR"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"

NASDAQ_LISTED_CACHE="$ROOT_DIR/data/universe/nasdaqlisted.txt"
OTHER_LISTED_CACHE="$ROOT_DIR/data/universe/otherlisted.txt"
US_SYMBOLS_CACHE="$ROOT_DIR/data/universe/us_symbols.csv"
MONTHLY_EXCLUDED_JSON="$ROOT_DIR/data/universe/monthly_excluded_symbols.json"
MANUAL_EXCLUDE_PATH="$ROOT_DIR/config/exclude_symbols.txt"
if [[ ! -f "$NASDAQ_LISTED_CACHE" || ! -f "$OTHER_LISTED_CACHE" || ! -f "$US_SYMBOLS_CACHE" || ! -f "$MONTHLY_EXCLUDED_JSON" ]]; then
  echo "Local universe cache or monthly exclusions missing; refreshing once before scan." >&2
  "$PYTHON_BIN" "$ROOT_DIR/scripts/update_symbol_universe.py"
fi

UNIVERSE_SHARDS="${HERMES_SCAN_UNIVERSE_SHARDS:-${HERMES_SCAN_SHARDS:-12}}"
WORKER_CONCURRENCY="${HERMES_SCAN_WORKER_CONCURRENCY:-6}"
PER_WORKER_STAGE2_SHARDS="${HERMES_SCAN_STAGE2_SHARDS_PER_WORKER:-1}"
STAGE1_BATCH="${HERMES_SCAN_STAGE1_BATCH:-90}"
STAGE2_BATCH="${HERMES_SCAN_STAGE2_BATCH:-120}"
STAGE1_PERIOD="${HERMES_SCAN_STAGE1_PERIOD:-1mo}"
STAGE2_PERIOD="${HERMES_SCAN_STAGE2_PERIOD:-3y}"
WORKER_STAGGER="${HERMES_SCAN_WORKER_STAGGER:-0.5}"
MAX_SYMBOLS="${HERMES_SCAN_MAX_SYMBOLS:-}"

export ROOT_DIR OUT_DIR RUN_DIR ARTIFACT_ROOT JSON_PATH MD_PATH COMBINED_STAGE1 COMBINED_STDERR
export NASDAQ_LISTED_CACHE OTHER_LISTED_CACHE US_SYMBOLS_CACHE MONTHLY_EXCLUDED_JSON MANUAL_EXCLUDE_PATH UNIVERSE_SHARDS WORKER_CONCURRENCY PER_WORKER_STAGE2_SHARDS STAGE1_BATCH STAGE2_BATCH STAGE1_PERIOD STAGE2_PERIOD WORKER_STAGGER MAX_SYMBOLS

"$PYTHON_BIN" - <<'PY'
import os
import sys
from pathlib import Path

root = Path(os.environ['ROOT_DIR'])
sys.path.insert(0, str(root))
import us_pattern_scan as scan  # noqa: E402

artifact_root = Path(os.environ['ARTIFACT_ROOT'])
max_symbols_raw = os.environ.get('MAX_SYMBOLS', '').strip()
max_symbols = int(max_symbols_raw) if max_symbols_raw else 0
us_symbols_cache = Path(os.environ['US_SYMBOLS_CACHE'])
monthly_excluded_json = Path(os.environ['MONTHLY_EXCLUDED_JSON'])
manual_exclude_path = Path(os.environ['MANUAL_EXCLUDE_PATH'])
if us_symbols_cache.exists():
    uni = scan.pd.read_csv(us_symbols_cache)
else:
    nasdaq_text = Path(os.environ['NASDAQ_LISTED_CACHE']).read_text(encoding='utf-8')
    other_text = Path(os.environ['OTHER_LISTED_CACHE']).read_text(encoding='utf-8')
    nasdaq = scan.parse_nasdaq_listed(nasdaq_text)
    other = scan.parse_other_listed(other_text)
    uni = scan.pd.concat([nasdaq, other], ignore_index=True)
    uni = uni.drop_duplicates(subset=['Symbol']).reset_index(drop=True)
    uni['keep'] = uni.apply(lambda r: scan.is_regular_security(r['Symbol'], r['name'], bool(r['etf']), bool(r['test_issue'])), axis=1)
    uni = uni[uni['keep']].copy()
manual_exclusions = set()
if manual_exclude_path.exists():
    for raw in manual_exclude_path.read_text(encoding='utf-8').splitlines():
        line = raw.strip().upper()
        if not line or line.startswith('#'):
            continue
        manual_exclusions.add(line)
        manual_exclusions.add(line.replace('-', '.'))
        manual_exclusions.add(line.replace('.', '-'))
generated_exclusions = set()
if monthly_excluded_json.exists():
    payload = scan.json.loads(monthly_excluded_json.read_text(encoding='utf-8'))
    for sym in payload.get('generated_symbols', []) or []:
        s = str(sym).strip().upper()
        if not s:
            continue
        generated_exclusions.add(s)
        generated_exclusions.add(s.replace('-', '.'))
        generated_exclusions.add(s.replace('.', '-'))
uni['Symbol'] = uni['Symbol'].astype(str).str.upper()
pre_filter_count = len(uni)
uni = uni[~uni['Symbol'].isin(manual_exclusions | generated_exclusions)].copy().reset_index(drop=True)
if max_symbols and max_symbols > 0:
    uni = uni.head(max_symbols).copy()
all_symbols = uni['Symbol'].tolist()
(artifact_root / 'all_symbols.txt').write_text('\n'.join(all_symbols) + ('\n' if all_symbols else ''), encoding='utf-8')
print(f'Prepared universe: {len(all_symbols)} symbols (pre-filter {pre_filter_count}, manual excludes {len(manual_exclusions)}, generated excludes {len(generated_exclusions)})')
PY

mapfile -t SHARD_SYMBOL_FILES < <("$PYTHON_BIN" - <<'PY'
import os
import sys
from pathlib import Path

root = Path(os.environ['ROOT_DIR'])
sys.path.insert(0, str(root))
import us_pattern_scan as scan  # noqa: E402

artifact_root = Path(os.environ['ARTIFACT_ROOT'])
universe_shards = max(1, int(os.environ.get('UNIVERSE_SHARDS', '1')))
symbols = [line.strip() for line in (artifact_root / 'all_symbols.txt').read_text(encoding='utf-8').splitlines() if line.strip()]
for idx, shard in enumerate(scan.split_into_shards(symbols, universe_shards), start=1):
    path = artifact_root / f'symbols_{idx:02d}.txt'
    path.write_text('\n'.join(shard) + ('\n' if shard else ''), encoding='utf-8')
    print(path)
PY
)

WORKER_DIRS=()
ACTIVE_JOBS=0
FAILURES=0
WORKER_TOTAL="${#SHARD_SYMBOL_FILES[@]}"
export WORKER_TOTAL
for ((i=0; i<WORKER_TOTAL; i++)); do
  WORKER_INDEX=$((i+1))
  WORKER_DIR="$ARTIFACT_ROOT/worker_$(printf '%02d' "$WORKER_INDEX")"
  mkdir -p "$WORKER_DIR"
  WORKER_DIRS+=("$WORKER_DIR")
  SYMBOLS_FILE="${SHARD_SYMBOL_FILES[$i]}"
  (
    export WORKER_INDEX SYMBOLS_FILE WORKER_DIR
    "$PYTHON_BIN" - <<'PY'
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

root = Path(os.environ['ROOT_DIR'])
sys.path.insert(0, str(root))
import us_pattern_scan as scan  # noqa: E402

worker_index = int(os.environ['WORKER_INDEX'])
worker_total = int(os.environ.get('WORKER_TOTAL', '1'))
worker_dir = Path(os.environ['WORKER_DIR'])
symbols_file = Path(os.environ['SYMBOLS_FILE'])
stderr_path = worker_dir / 'consolidation_scan.stderr.log'
original_symbols = [line.strip() for line in symbols_file.read_text(encoding='utf-8').splitlines() if line.strip()]
mapped = {scan.yahoo_symbol(sym): sym for sym in original_symbols}
yahoo_symbols = list(mapped.keys())
stage1_period = os.environ['STAGE1_PERIOD']
stage1_batch = int(os.environ['STAGE1_BATCH'])
stage2_batch = int(os.environ['STAGE2_BATCH'])
per_worker_stage2_shards = max(1, int(os.environ['PER_WORKER_STAGE2_SHARDS']))
open(stderr_path, 'w').close()

# 缓存实例
cache_dir = Path(os.environ['ARTIFACT_ROOT']) / '.consolidation_cache'
cache = scan.ConsolidationCache(cache_dir, str(stderr_path))

scan.append_log(str(stderr_path), f"WORKER_START worker={worker_index}/{worker_total} universe={len(yahoo_symbols)} symbols_file={symbols_file.name}")

# 跳过 stage1，直接使用所有符号作为 liquid
liquid = yahoo_symbols
stage1_summary = {
    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
    'worker': worker_index,
    'worker_total': worker_total,
    'universe_total': len(yahoo_symbols),
    'liquid_count': len(liquid),
    'stage1_misses': 0,
    'liquid_symbols': liquid,
    'universe_source': 'shared_cache',
    'stage1_period': 'skipped',
    'stage1_batch': stage1_batch,
    'stage2_batch': stage2_batch,
    'stage2_shards_per_worker': per_worker_stage2_shards,
    'symbols_file': str(symbols_file),
}
(worker_dir / 'worker_stage1.json').write_text(json.dumps(stage1_summary, ensure_ascii=False, indent=2), encoding='utf-8')
scan.append_log(str(stderr_path), f"WORKER_STAGE1_DONE worker={worker_index}/{worker_total} universe={len(yahoo_symbols)} liquid={len(liquid)} misses=0")

results = []
consolidating_count = 0
breaking_out_count = 0
deep_scan_count = 0
miss2 = set()
shard_summaries = []

# 计算增量下载日期：过去7天
today = datetime.now(timezone.utc).date()
start_date = (today - timedelta(days=7)).strftime('%Y-%m-%d')
end_date = today.strftime('%Y-%m-%d')

# 分组：有缓存 vs 无缓存
cached_symbols = []
uncached_symbols = []
for ys in liquid:
    if cache.get_last_date(ys) is not None:
        cached_symbols.append(ys)
    else:
        uncached_symbols.append(ys)

# 下载增量数据（有缓存的符号）
if cached_symbols:
    scan.append_log(str(stderr_path), f"WORKER_INCR_DOWNLOAD worker={worker_index} symbols={len(cached_symbols)} start={start_date} end={end_date}")
    stage2_inc, miss_inc = scan.download_bars(
        cached_symbols,
        period=None,
        stderr_path=str(stderr_path),
        batch=stage2_batch,
        interval='1wk',
        phase=f'WORKER_{worker_index:02d}_INCR',
        start_date=start_date,
        end_date=end_date
    )
    # 合并到缓存
    for ys, df in stage2_inc.items():
        if ys not in miss_inc:
            df_merged = cache.merge_incremental(ys, df)
            cache.put(ys, df_merged)
    miss_set = set(miss_inc)
else:
    miss_set = set()

# 下载完整数据（无缓存的符号）
if uncached_symbols:
    scan.append_log(str(stderr_path), f"WORKER_FULL_DOWNLOAD worker={worker_index} symbols={len(uncached_symbols)}")
    stage2_full, miss_full = scan.download_bars(
        uncached_symbols,
        period='3y',
        stderr_path=str(stderr_path),
        batch=stage2_batch,
        interval='1wk',
        phase=f'WORKER_{worker_index:02d}_FULL'
    )
    # 存入缓存
    for ys, df in stage2_full.items():
        if ys not in miss_full:
            cache.put(ys, df)
    miss_set.update(miss_full)

# 最后，从缓存中读取所有符号的DataFrame用于扫描
stage2 = {}
for ys in liquid:
    df = cache.get_cached(ys, max_age_days=9999)  # 从缓存读取
    if df is not None and len(df) >= 52:
        stage2[ys] = df
    else:
        miss_set.add(ys)

shard_results, cons_list, break_list = scan.scan_stage2_dataset(stage2, mapped, str(stderr_path), cache)
deep_scan_count += len(stage2)
miss2.update(miss_set)
results.extend(shard_results)
consolidating_count += len(cons_list)
breaking_out_count += len(break_list)
shard_summary = {
    'shard': 1,
    'input_symbols': len(liquid),
    'downloaded_symbols': len(stage2),
    'misses': len(miss_set),
    'candidates': len(shard_results),
    'consolidating': len(cons_list),
    'breaking_out': len(break_list),
}
shard_summaries.append(shard_summary)
shard_path = worker_dir / f'shard_01.json'
shard_path.write_text(
    json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'summary': shard_summary,
        'deep_scan_count': len(stage2),
        'results': shard_results,
        'miss_symbols': sorted(list(miss_set)),
    }, ensure_ascii=False, indent=2, default=str),
    encoding='utf-8',
)
scan.append_log(str(stderr_path), f"WORKER_SCAN_DONE worker={worker_index}/{worker_total} downloaded={len(stage2)} misses={len(miss_set)} candidates={len(shard_results)}")

worker_result = {
    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
    'worker': worker_index,
    'worker_total': worker_total,
    'stage1_summary': stage1_summary,
    'deep_scan_count': deep_scan_count,
    'stage2_misses': len(miss2),
    'candidate_total': len(results),
    'consolidating': consolidating_count,
    'breaking_out': breaking_out_count,
    'shard_count': len(shard_summaries),
}
(worker_dir / 'result.json').write_text(json.dumps(worker_result, ensure_ascii=False, indent=2), encoding='utf-8')
scan.append_log(str(stderr_path), f"WORKER_DONE worker={worker_index}/{worker_total} deep_scan={deep_scan_count} candidates={len(results)}")
PY
  ) &
  ACTIVE_JOBS=$((ACTIVE_JOBS+1))
  sleep "$WORKER_STAGGER"
  if (( ACTIVE_JOBS >= WORKER_CONCURRENCY )); then
    if ! wait -n; then
      FAILURES=$((FAILURES+1))
    fi
    ACTIVE_JOBS=$((ACTIVE_JOBS-1))
  fi
done

while (( ACTIVE_JOBS > 0 )); do
  if ! wait -n; then
    FAILURES=$((FAILURES+1))
  fi
  ACTIVE_JOBS=$((ACTIVE_JOBS-1))
done

WORKER_DIRS_JOINED="$(printf '%s\n' "${WORKER_DIRS[@]}")"
export WORKER_DIRS_JOINED FAILURES

"$PYTHON_BIN" - <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ['ROOT_DIR'])
sys.path.insert(0, str(root))
import us_pattern_scan as scan  # noqa: E402

worker_dirs = [Path(x) for x in os.environ['WORKER_DIRS_JOINED'].splitlines() if x.strip()]
combined_stage1 = Path(os.environ['COMBINED_STAGE1'])
combined_stderr = Path(os.environ['COMBINED_STDERR'])
json_path = Path(os.environ['JSON_PATH'])

stage1_payloads = []
all_liquid = []
stderr_chunks = []
results = []
miss2 = set()
shard_summaries = []
long_count = 0
short_count = 0
deep_scan_count = 0
original_symbols = []
original_seen = set()

for worker_dir in worker_dirs:
    stage1_path = worker_dir / 'worker_stage1.json'
    if stage1_path.exists():
        payload = json.loads(stage1_path.read_text(encoding='utf-8'))
        stage1_payloads.append(payload)
        all_liquid.extend(payload.get('liquid_symbols') or [])
        symbols_file = payload.get('symbols_file')
        if symbols_file and Path(symbols_file).exists():
            for sym in Path(symbols_file).read_text(encoding='utf-8').splitlines():
                sym = sym.strip()
                if sym and sym not in original_seen:
                    original_seen.add(sym)
                    original_symbols.append(sym)
    stderr_path = worker_dir / 'consolidation_scan.stderr.log'
    if stderr_path.exists():
        stderr_chunks.append(f"===== {worker_dir.name} =====\n" + stderr_path.read_text(encoding='utf-8'))
    worker_name = worker_dir.name
    worker_num = int(worker_name.split('_')[-1]) if '_' in worker_name and worker_name.split('_')[-1].isdigit() else None
    for path in sorted(worker_dir.glob('shard_*.json')):
        payload = json.loads(path.read_text(encoding='utf-8'))
        summary = dict(payload.get('summary') or {})
        summary['worker'] = worker_num
        summary['worker_label'] = worker_name
        summary['local_shard'] = int(summary.get('shard') or 0)
        summary['shard'] = len(shard_summaries) + 1
        shard_summaries.append(summary)
        results.extend(payload.get('results') or [])
        miss2.update(payload.get('miss_symbols') or [])
        deep_scan_count += int(payload.get('deep_scan_count') or 0)
        # all are long (做多)
        long_count += int(summary.get('candidates') or 0)

if not stage1_payloads:
    raise SystemExit('No worker_stage1.json files were produced; cannot aggregate consolidation run')

unique_liquid = []
seen = set()
for sym in all_liquid:
    if sym and sym not in seen:
        seen.add(sym)
        unique_liquid.append(sym)

stage1_summary = {
    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
    'universe_total': sum(int(p.get('universe_total') or 0) for p in stage1_payloads),
    'liquid_count': len(unique_liquid),
    'stage1_misses': sum(int(p.get('stage1_misses') or 0) for p in stage1_payloads),
    'liquid_symbols': unique_liquid,
    'universe_source': 'local_cache',
    'run_config': {
        'mode': 'consolidation multi-worker',
        'universe_shards': int(os.environ.get('UNIVERSE_SHARDS', '0') or 0),
        'worker_concurrency': int(os.environ.get('WORKER_CONCURRENCY', '0') or 0),
        'stage1_period': os.environ.get('STAGE1_PERIOD', ''),
        'stage1_batch': int(os.environ.get('STAGE1_BATCH', '0') or 0),
        'stage2_batch': int(os.environ.get('STAGE2_BATCH', '0') or 0),
        'stage2_shards_per_worker': int(os.environ.get('PER_WORKER_STAGE2_SHARDS', '0') or 0),
        'worker_stagger_seconds': float(os.environ.get('WORKER_STAGGER', '0') or 0),
        'max_symbols': os.environ.get('MAX_SYMBOLS', '') or 'all',
    },
    'workers': [
        {
            'worker': int(p.get('worker') or 0),
            'universe_total': int(p.get('universe_total') or 0),
            'liquid_count': int(p.get('liquid_count') or 0),
            'stage1_misses': int(p.get('stage1_misses') or 0),
        }
        for p in stage1_payloads
    ],
}
combined_stage1.write_text(json.dumps(stage1_summary, ensure_ascii=False, indent=2), encoding='utf-8')
combined_stderr.write_text('\n\n'.join(stderr_chunks), encoding='utf-8')

results.sort(key=lambda x: (x['score'], x['_duration_weeks']), reverse=True)
deduped = []
seen_symbols = set()
for row in results:
    if row['symbol'] in seen_symbols:
        continue
    deduped.append(row)
    seen_symbols.add(row['symbol'])

consolidating = [r for r in deduped if not r['_is_breakout']]
breaking_out = [r for r in deduped if r['_is_breakout']]
consolidating.sort(key=lambda x: (x['score'], x['_duration_weeks']), reverse=True)
breaking_out.sort(key=lambda x: (x['score'], x['_duration_weeks']), reverse=True)

top10_cons = consolidating[:10]
top10_break = breaking_out[:10]

out = {
    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
    'data_sources': [
        'Nasdaq Trader nasdaqlisted.txt',
        'Nasdaq Trader otherlisted.txt',
        'Yahoo Finance / yfinance 周线 OHLCV (3年)',
    ],
    'universe_total': int(len(original_symbols)),
    'liquid_count': int(len(unique_liquid)),
    'deep_scan_count': int(deep_scan_count),
    'stage1_misses': int(stage1_summary['stage1_misses']),
    'stage2_misses': int(len(miss2)),
    'candidate_total': int(len(results)),
    'consolidating_count': int(len(consolidating)),
    'breaking_out_count': int(len(breaking_out)),
    'stderr_log': str(combined_stderr),
    'artifact_dir': str(Path(os.environ['ARTIFACT_ROOT'])),
    'shard_count': len(shard_summaries),
    'shards': shard_summaries,
    'top10_consolidating': top10_cons,
    'top10_breaking_out': top10_break,
}
json_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
print(json.dumps({
    'worker_count': len(worker_dirs),
    'worker_failures': int(os.environ.get('FAILURES', '0') or 0),
    'combined_stage1': str(combined_stage1),
    'combined_stderr': str(combined_stderr),
    'json_path': str(json_path),
}, ensure_ascii=False, indent=2))
PY

JSON_FINAL_PATH="$FINAL_DIR/consolidation_scan.json"
MD_FINAL_PATH="$FINAL_DIR/consolidation_scan.md"
cp "$JSON_PATH" "$JSON_FINAL_PATH"

"$PYTHON_BIN" "$ROOT_DIR/scripts/render_report.py" "$JSON_FINAL_PATH" "$MD_FINAL_PATH"
cp "$MD_FINAL_PATH" "$MD_PATH"

printf '\n\n---\n產物目錄：%s\n最終輸出目錄：%s\nMarkdown：%s\nJSON：%s\n日誌：%s\n合併stage1：%s\nWorker 目錄：%s\n' \
  "$RUN_DIR" "$FINAL_DIR" "$MD_FINAL_PATH" "$JSON_FINAL_PATH" "$COMBINED_STDERR" "$COMBINED_STAGE1" "$ARTIFACT_ROOT"