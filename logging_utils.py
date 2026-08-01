# logging_utils.py
import sys
from datetime import datetime, timezone

def log_info(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] INFO: {msg}", flush=True)

def log_warning(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] WARNING: {msg}", flush=True, file=sys.stderr)

def log_error(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] ERROR: {msg}", flush=True, file=sys.stderr)

def log_debug(msg):
    # 默认不输出debug
    pass