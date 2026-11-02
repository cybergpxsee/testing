"""解析 Yahoo 过往股价 HTML 存档 -> OHLCV DataFrame（供点时回放用）。

表格结构（繁体中文界面）：日期 | 開盤價 | 最高價 | 最低價 | 收盤價 | 調整結算價 | 成交量
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FOLDER = Path(r'D:\Trading Course\P\screen shot')
HISTORY_DIR = Path(os.environ.get('HISTORY_DIR') or
                   (Path(__file__).resolve().parent.parent / 'data' / 'history'))

MONTHS = {m: i for i, m in enumerate(
    ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'], start=1)}


def parse_cn_date(text: str) -> pd.Timestamp | None:
    t = text.strip().replace(' ', '')
    m = re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$', t)
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', t)
    if m:
        return pd.Timestamp(*map(int, m.groups()))
    return None


def num(text: str) -> float:
    t = text.strip().replace(',', '').replace('%', '')
    try:
        return float(t)
    except ValueError:
        return np.nan


def parse_html(path: Path) -> pd.DataFrame:
    html = path.read_text(encoding='utf-8', errors='replace')
    i = html.find('<tbody')
    if i < 0:
        raise ValueError(f'no tbody in {path.name}')
    j = html.find('</tbody>', i)
    chunk = html[i:j if j > 0 else len(html)]
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', chunk, flags=re.S)
    recs = []
    for r in rows:
        cells = [re.sub(r'<[^>]+>', '', c) for c in re.findall(r'<td[^>]*>(.*?)</td>', r, flags=re.S)]
        if len(cells) < 7:
            continue
        d = parse_cn_date(cells[0])
        if d is None:
            continue
        o, h, l, c, adj, vol = (num(x) for x in cells[1:7])
        if not all(np.isfinite([o, h, l, c])):
            continue
        recs.append({'Date': d, 'Open': o, 'High': h, 'Low': l, 'Close': c, 'Adj Close': adj, 'Volume': vol})
    df = pd.DataFrame(recs)
    if df.empty:
        raise ValueError(f'no rows parsed from {path.name}')
    df = df.dropna(subset=['Date']).sort_values('Date').reset_index(drop=True)
    df['Volume'] = df['Volume'].fillna(0)
    return df


def load_history_csvs(folder: Path | None = None) -> dict[str, pd.DataFrame]:
    """读取 scripts/fetch_history.py（GitHub fetch-history 工作流）导出的 CSV。
    列：Date, Open, High, Low, Close, Volume（复权价，口径接近 TradingView 图）。"""
    folder = Path(folder) if folder else HISTORY_DIR
    out: dict[str, pd.DataFrame] = {}
    if not folder.exists():
        return out
    for path in sorted(folder.glob('*.csv')):
        if path.name == 'manifest.csv':
            continue
        try:
            df = pd.read_csv(path)
            cols = {c.lower().replace(' ', '_'): c for c in df.columns}
            date_col = cols.get('date') or cols.get('datetime')
            if not date_col:
                continue
            df[date_col] = pd.to_datetime(df[date_col]).dt.tz_localize(None)
            ren = {}
            for want in ('open', 'high', 'low', 'close', 'volume'):
                if want in cols and cols[want] != want:
                    ren[cols[want]] = want.title()
            df = df.rename(columns=ren).rename(columns={date_col: 'Date'})
            keep = ['Date'] + [c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c in df.columns]
            df = df[keep].dropna(subset=['Open', 'High', 'Low', 'Close']).sort_values('Date').reset_index(drop=True)
            if 'Volume' not in df.columns:
                df['Volume'] = 0.0
            if len(df) >= 60:
                out[path.stem.upper()] = df
        except Exception as exc:
            print(f'!! {path.name}: {exc}')
    return out


def load_all() -> dict[str, pd.DataFrame]:
    """回放用数据源：CSV 导出（全区间）优先，HTML 存档（只有最近一页）补缺。"""
    data = {}
    for sym, df in load_all_html().items():
        data[sym] = df
    for sym, df in load_history_csvs().items():
        cur = data.get(sym)
        if cur is None or len(df) >= len(cur):
            data[sym] = df
    return data


def load_all_html() -> dict[str, pd.DataFrame]:
    out = {}
    for path in sorted(FOLDER.glob('*.html')):
        m = re.search(r'\(([^()]+)\)\s*過往', path.name)
        sym = (m.group(1) if m else path.stem).upper()
        try:
            out[sym] = parse_html(path)
        except Exception as exc:
            print(f'!! {path.name}: {exc}')
    return out


if __name__ == '__main__':
    data = load_all()
    for sym, df in data.items():
        adv = (df['Close'] * df['Volume']).rolling(20).mean().iloc[-1]
        print(f"{sym:<12} rows={len(df):4d}  {df['Date'].min().date()} -> {df['Date'].max().date()}  "
              f"close={df['Close'].iloc[-1]:>9.2f}  20日均成交额={adv/1e6:>9.1f}M")
