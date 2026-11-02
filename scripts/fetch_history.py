#!/usr/bin/env python3
"""
抓取指定标的的完整日线历史 -> data/history/<SYMBOL>.csv（+ 同名 .pkl 供离线回放）

用途：给形态判定做点时回放/标定。日期区间按用户截图的显示范围设置，
确保「截图里的圆圈」落在导出数据窗口内（Yahoo 存档网页只带最近一页，所以之前的
HTML 解析对 TSLA(2024)/棉花(2023-2024) 完全对不上）。

取数方式与生产扫描一致（yfinance / Yahoo Finance），但这里用 auto_adjust=True，
以便导出的价格口径贴近 TradingView 图上看到的数值（图通常用复权价）。

用法：
    python scripts/fetch_history.py                       # 抓全部预设标的
    python scripts/fetch_history.py --symbols HOOD,TSLA   # 只抓指定标的
    python scripts/fetch_history.py --out-dir data/history
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent

# (symbol, start, end, 说明) —— 区间覆盖对应用户截图的显示范围
TARGETS = [
    ('HOOD',      '2025-08-01', '2026-07-01', '截图 2025-09→2026-08；圈约 2026-05'),
    ('TSLA',      '2023-09-01', '2025-01-31', '截图 2023-11→2024-11；两个圈约 2024-04 / 2024-05'),
    ('SIRI',      '2024-09-01', '2026-08-01', '截图 2024-10→2026-07；圈约 2026-02'),
    ('MRNA',      '2024-09-01', '2026-02-01', '截图 2024-11→2026-01；圈约 2025-12'),
    ('P',         '2025-06-01', '2026-08-01', '截图 2025-07→2026-07；圈约 2026-06'),
    ('BTC-USD',   '2022-02-01', '2023-05-01', '截图 2022-03→2023-04；圈约 2023-03'),
    ('CTK28.NYB', '2023-01-01', '2024-07-01', '截图 2023-02→2024-05；圈约 2024-03'),
    # 棉花主连做对照：主连合约成交量非零，可验证「筹码密集区」在期货上的可用性
    ('CT28.NYB',  '2023-01-01', '2024-07-01', '棉花主连（对照 CTK28.NYB 成交量为 0 的问题）'),
    ('CT.NYB',    '2023-01-01', '2024-07-01', '棉花（另一种 Yahoo 写法，抓不到就忽略）'),
]


def fetch_one(symbol: str, start: str, end: str, tries: int = 3) -> pd.DataFrame | None:
    for attempt in range(1, tries + 1):
        try:
            df = yf.download(symbol, start=start, end=end, interval='1d',
                             auto_adjust=True, progress=False, threads=False, timeout=30)
            if df is not None and len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                df = df.reset_index()
                df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)
                return df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]
        except Exception as exc:
            print(f'  attempt {attempt} failed for {symbol}: {exc}')
        time.sleep(1.5 * attempt)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', default=str(ROOT / 'data' / 'history'))
    ap.add_argument('--symbols', default='', help='逗号分隔，留空=用预设 TARGETS')
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(',') if s.strip()}
        targets = [t for t in TARGETS if t[0].upper() in wanted] or \
                  [(s, '2022-01-01', '2026-12-31', 'ad-hoc') for s in wanted]
    else:
        targets = TARGETS

    summary = []
    for sym, start, end, note in targets:
        print(f'fetch {sym} {start}..{end}  ({note})')
        df = fetch_one(sym, start, end)
        if df is None or df.empty:
            print(f'  !! {sym}: no data')
            summary.append({'symbol': sym, 'rows': 0, 'status': 'FAILED', 'note': note})
            continue
        csv_path = out_dir / f'{sym}.csv'
        df.to_csv(csv_path, index=False)
        (out_dir / f'{sym}.pkl').write_bytes(pickle.dumps(df))
        vol_total = float(df['Volume'].sum())
        print(f'  ok rows={len(df)} {df["Date"].iloc[0].date()} -> {df["Date"].iloc[-1].date()} '
              f'volume_sum={"0" if vol_total <= 0 else "nonzero"}')
        summary.append({'symbol': sym, 'rows': len(df),
                        'first': str(df['Date'].iloc[0].date()), 'last': str(df['Date'].iloc[-1].date()),
                        'volume_nonzero': vol_total > 0, 'status': 'OK', 'note': note})
        time.sleep(0.6)

    man = pd.DataFrame(summary)
    man.to_csv(out_dir / 'manifest.csv', index=False)
    (out_dir / 'manifest.json').write_bytes(pickle.dumps({
        'generated_at_utc': pd.Timestamp.utcnow().isoformat(),
        'targets': [list(t) for t in targets], 'summary': summary}))
    print('\nSUMMARY')
    print(man.to_string(index=False))
    ok = sum(1 for s in summary if s['status'] == 'OK')
    print(f'\n{ok}/{len(summary)} symbols exported to {out_dir}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
