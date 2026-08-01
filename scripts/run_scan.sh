#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${HERMES_SCAN_OUT_DIR:-$ROOT_DIR/output}"
PYTHON_BIN="${HERMES_SCAN_PYTHON:-python}"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="$OUT_DIR/$STAMP"
STDERR_PATH="$ARTIFACT_DIR/us_pattern_scan_stderr.log"
mkdir -p "$ARTIFACT_DIR"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"

# Run the CLI - always output markdown to file, json to artifact
FORMAT="${HERMES_SCAN_FORMAT:-markdown}"

if [[ "$FORMAT" == "markdown" ]]; then
    # Output markdown to file and stdout
    "$PYTHON_BIN" "$ROOT_DIR/scan_cli.py" \
        --format "markdown" \
        --max-symbols "${HERMES_SCAN_MAX_SYMBOLS:-0}" \
        --stderr-path "$STDERR_PATH" \
        --shards "${HERMES_SCAN_SHARDS:-4}" \
        --artifact-dir "$ARTIFACT_DIR" \
        --stage1-period "${HERMES_SCAN_STAGE1_PERIOD:-1mo}" \
        --stage1-batch "${HERMES_SCAN_STAGE1_BATCH:-90}" \
        --stage2-batch "${HERMES_SCAN_STAGE2_BATCH:-120}" \
        | tee "$ARTIFACT_DIR/pullback_scan.md"
else
    # Output JSON to artifact
    "$PYTHON_BIN" "$ROOT_DIR/scan_cli.py" \
        --format "json" \
        --max-symbols "${HERMES_SCAN_MAX_SYMBOLS:-0}" \
        --stderr-path "$STDERR_PATH" \
        --shards "${HERMES_SCAN_SHARDS:-4}" \
        --artifact-dir "$ARTIFACT_DIR" \
        --stage1-period "${HERMES_SCAN_STAGE1_PERIOD:-1mo}" \
        --stage1-batch "${HERMES_SCAN_STAGE1_BATCH:-90}" \
        --stage2-batch "${HERMES_SCAN_STAGE2_BATCH:-120}"
fi

echo "Scan complete. Artifacts in: $ARTIFACT_DIR"