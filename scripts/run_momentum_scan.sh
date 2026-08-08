#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/output}"
PYTHON_BIN="${MOMENTUM_SCAN_PYTHON:-python}"
mkdir -p "$OUT_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$OUT_DIR/$STAMP"
ARTIFACT_ROOT="$RUN_DIR/artifacts"
mkdir -p "$ARTIFACT_ROOT"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"

STAGE1_PERIOD="${MOMENTUM_SCAN_STAGE1_PERIOD:-1mo}"
STAGE1_BATCH="${MOMENTUM_SCAN_STAGE1_BATCH:-120}"
STAGE2_BATCH="${MOMENTUM_SCAN_STAGE2_BATCH:-100}"
STAGE2_PERIOD="${MOMENTUM_SCAN_STAGE2_PERIOD:-1y}"
SHARDS="${MOMENTUM_SCAN_SHARDS:-4}"
MAX_SYMBOLS="${MOMENTUM_SCAN_MAX_SYMBOLS:-}"
ARTIFACT_DIR="$OUT_DIR"

echo "=== Momentum Rank Scan ==="
echo "Run dir: $RUN_DIR"
echo "Stage1: period=$STAGE1_PERIOD batch=$STAGE1_BATCH"
echo "Stage2: period=$STAGE2_PERIOD batch=$STAGE2_BATCH"
echo "Shards: $SHARDS"
echo "Max symbols: ${MAX_SYMBOLS:-all}"

# 直接調用 momentum_rank_scanner.py
"$PYTHON_BIN" "$ROOT_DIR/scripts/momentum_rank_scanner.py" \
    --format markdown \
    --stderr-path "$RUN_DIR/momentum_scan.log" \
    --artifact-dir "$ARTIFACT_DIR" \
    --shards "$SHARDS" \
    --stage1-period "$STAGE1_PERIOD" \
    --stage1-batch "$STAGE1_BATCH" \
    --stage2-batch "$STAGE2_BATCH" \
    --stage2-period "$STAGE2_PERIOD" \
    ${MAX_SYMBOLS:+--max-symbols "$MAX_SYMBOLS"}

SCAN_EXIT_CODE=$?

# 生成 Discord embed
LATEST_JSON=$(find "$ARTIFACT_DIR" -name 'momentum_rank_output.json' | sort | tail -n 1)
if [[ -n "$LATEST_JSON" && -f "$LATEST_JSON" ]]; then
    "$PYTHON_BIN" "$ROOT_DIR/scripts/render_momentum_report.py" "$LATEST_JSON" "$ARTIFACT_DIR"
    echo "Generated Discord embed: $ARTIFACT_DIR/momentum_discord_embed.json"
    echo "Generated Markdown report: $ARTIFACT_DIR/momentum_rank_report.md"
else
    echo "ERROR: momentum_rank_output.json not found"
    # 印出 log 以利除錯
    if [[ -f "$RUN_DIR/momentum_scan.log" ]]; then
        echo "=== momentum_scan.log ==="
        cat "$RUN_DIR/momentum_scan.log"
    fi
    exit 1
fi

# 如果 Python 回傳非零，印出 log 並失敗
if [[ $SCAN_EXIT_CODE -ne 0 ]]; then
    echo "=== Scanner exited with code $SCAN_EXIT_CODE ==="
    if [[ -f "$RUN_DIR/momentum_scan.log" ]]; then
        echo "=== momentum_scan.log ==="
        cat "$RUN_DIR/momentum_scan.log"
    fi
    # 即使 scanner 失敗，若已產出 JSON 仍繼續處理（可能包含錯誤資訊）
    # 這裡選擇不強制 exit，讓後續步驟能上傳 artifact
fi

# 複製到最終輸出目錄
FINAL_DIR="$OUT_DIR/final"
mkdir -p "$FINAL_DIR"
cp "$ARTIFACT_DIR/momentum_rank_output.json" "$FINAL_DIR/"
cp "$ARTIFACT_DIR/momentum_rank_report.md" "$FINAL_DIR/"
cp "$ARTIFACT_DIR/momentum_discord_embed.json" "$FINAL_DIR/"

echo ""
echo "=== Scan Complete ==="
echo "Artifacts: $ARTIFACT_DIR"
echo "Final output: $FINAL_DIR"
echo "Markdown: $FINAL_DIR/momentum_rank_report.md"
echo "JSON: $FINAL_DIR/momentum_rank_output.json"
echo "Discord: $FINAL_DIR/momentum_discord_embed.json"
echo "Log: $RUN_DIR/momentum_scan.log"