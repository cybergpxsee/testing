"""把妙想(mx_us_finance_data)返回的 JSON 入库为 data/history/<SYMBOL>.csv。

妙想的返回结构：data[] 里两个 sheet ——
  sheet A: items = [成交量, 最低价, 最高价, 收盘价]（列 = 日期）
  sheet B: items = [区间开盘价]（列 = 日期）
本工具按日期合并成 Date/Open/High/Low/Close/Volume，追加合并进已有 CSV（按日期去重）。

用法：
  1) 把 MCP 返回的 JSON 存成文件（例如 examples/mcp_out/TSLA_2024Q2.json）
  2) python scripts/ingest_mx.py examples/mcp_out/*.csv_dir_or_files --symbol TSLA
     或  python scripts/ingest_mx.py --dir examples/mcp_out
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'data' / 'history'

UNIT_RE = re.compile(r'(万美元|万股|万|亿美元|亿股|亿元|亿|美元|股|元|张|%)')


def clean_number(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    mult = 1.0
    if '亿' in s:
        mult = 1e8
    elif s.endswith('万') or '万' in s:
        mult = 1e4
    s = UNIT_RE.sub('', s).replace(',', '')
    try:
        return float(s) * mult
    except ValueError:
        return None


def norm_date(col: str) -> str | None:
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', str(col))
    return m.group(0) if m else None


FIELD_MAP = {'成交量': 'Volume', '最低价': 'Low', '最高价': 'High', '收盘价': 'Close',
             '开盘价': 'Open', '区间开盘价': 'Open', '最高价(美元)': 'High', '最低价(美元)': 'Low'}


def parse_mx_payload(payload: dict) -> pd.DataFrame:
    rows: dict[str, dict] = {}
    for sheet in payload.get('data') or []:
        cols = sheet.get('columns') or []
        dates = [norm_date(c) for c in cols[1:]]
        for item in sheet.get('items') or []:
            name = str(item[0]).strip()
            field = FIELD_MAP.get(name)
            if not field:
                continue
            for i, d in enumerate(dates):
                if not d:
                    continue
                val = clean_number(item[i + 1] if i + 1 < len(item) else None)
                if val is None:
                    continue
                rows.setdefault(d, {})[field] = val
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{'Date': d, **v} for d, v in rows.items()])
    df['Date'] = pd.to_datetime(df['Date'])
    return df.sort_values('Date').reset_index(drop=True)


def merge_write(symbol: str, new: pd.DataFrame) -> tuple[int, int]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f'{symbol}.csv'
    if path.exists():
        old = pd.read_csv(path)
        old['Date'] = pd.to_datetime(old['Date'])
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new
    combined = combined.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
    before = 0 if not path.exists() else len(pd.read_csv(path))
    combined.to_csv(path, index=False)
    return before, len(combined)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='*', help='JSON 文件（妙想返回原文）')
    ap.add_argument('--symbol', required=True, help='写入 data/history/<SYMBOL>.csv')
    args = ap.parse_args(argv)
    if not args.paths:
        raise SystemExit('need at least one JSON file')
    total = 0
    for p in args.paths:
        payload = json.loads(Path(p).read_text(encoding='utf-8'))
        df = parse_mx_payload(payload)
        if df.empty:
            print(f'  !! {p}: 无可解析行')
            continue
        missing = [c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c not in df.columns]
        before, after = merge_write(args.symbol.upper(), df)
        total += len(df)
        print(f'  {p}: {len(df)} 行 {df["Date"].iloc[0].date()}→{df["Date"].iloc[-1].date()} '
              f'缺字段={missing or "无"} | {args.symbol}.csv {before}→{after} 行')
    print(f'total ingested rows: {total}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
