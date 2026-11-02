"""点时回放 v2（line_scan 双线楔形）：逐根把当根当「最新一根」，看是否命中用户的圈。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import line_scan as L2               # noqa: E402
from parse_yahoo_html import load_all  # noqa: E402

CIRCLE_WINDOWS = {
    'HOOD':    ('2026-05-01', '2026-05-25', '下降线末端 ~73；圈后 5 月 28 日起爆拉到 94+'),
    'BTC-USD': ('2023-02-20', '2023-03-02', '18.8K 平台上方回踩 ~20.5K（存档最后一天 03-02）'),
    'P':       ('2026-06-08', '2026-06-18', '下降线末端 ~70；6/11 曾假摔到 64.80 又收回'),
    'MRNA':    ('2025-11-25', '2025-12-09', '下降线末端 ~25；随后涨到 36+'),
    'SIRI':    ('2026-02-10', '2026-03-20', '下降线末端 ~21.0；地板线 20.0，fib 0=23.59 / 1=19.91'),
}

MIN_BARS = 80      # HOOD 存档只有 89 行，调低才能回放（生产仍用 line_scan.MIN_BARS=90）
L2.MIN_BARS = MIN_BARS


def replay(sym, df, scan_fn):
    hits = []
    for end in range(MIN_BARS, len(df)):
        sub = df.iloc[:end + 1].reset_index(drop=True)
        r = scan_fn(sym, sub)
        if r:
            hits.append(r)
    return hits


def collapse(hits, gap_days=7):
    ev = []
    for h in hits:
        d = pd.Timestamp(h['pullback_date'])
        if ev and (d - ev[-1]['last']).days <= gap_days and abs(h.get('shelf_line', 0) - ev[-1]['row'].get('shelf_line', 0)) / max(ev[-1]['row'].get('shelf_line', 1), 1) < 0.06:
            ev[-1]['last'] = d
            ev[-1]['n'] += 1
        else:
            ev.append({'first': d, 'last': d, 'n': 1, 'row': h})
    return ev


def load_labels(path: Path = None) -> dict:
    """读 examples/labels.csv（用户确认的买点）。有文件就以它为准，覆盖内置估计窗口。"""
    path = Path(path) if path else Path(__file__).resolve().parent / 'labels.csv'
    if not path.exists():
        return {}
    out = {}
    try:
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip() or line.startswith('symbol'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 3:
                continue
            sym, d, side = parts[0].upper(), parts[1], parts[2]
            ts = pd.Timestamp(d)
            out.setdefault(sym, []).append((ts, side, ' '.join(parts[3:])))
    except Exception as exc:
        print(f'!! labels.csv: {exc}')
    return out


LABELS = load_labels()
LABEL_TOL_DAYS = int(os.environ.get('LABEL_TOL_DAYS', '12'))   # 交易日近似换算成日历天


def window_for(sym: str):
    """返回 (起, 止, 说明)。优先用 labels.csv。"""
    lab = LABELS.get(sym.upper())
    if lab:
        ts = [t for t, _s, _n in lab]
        lo, hi = min(ts) - pd.Timedelta(days=LABEL_TOL_DAYS), max(ts) + pd.Timedelta(days=LABEL_TOL_DAYS)
        note = '; '.join(f'{t.date()}{s} {n}'.strip() for t, s, n in lab)
        return str(lo.date()), str(hi.date()), note
    return CIRCLE_WINDOWS.get(sym)


def main():
    data = load_all()
    if LABELS:
        print(f'labels.csv 已载入：{ {k: [str(t.date()) for t, _s, _n in v] for k, v in LABELS.items()} }')
    else:
        print('labels.csv 不存在，使用内置的读图估计窗口（精度有限）')
    hits_total = misses_total = 0
    for sym, df in data.items():
        hits = replay(sym, df, L2.scan_line_long)
        ev = collapse(hits)
        win = window_for(sym)
        print(f'\n===== {sym} rows={len(df)} 区间 {df["Date"].min().date()}→{df["Date"].max().date()} 命中事件 {len(ev)} =====')
        if win:
            print(f'  用户圈窗口: {win[0]} ~ {win[1]}  ({win[2]})')
        matched = False
        for e in ev:
            r = e['row']
            flag = ''
            if win:
                overlap = e['first'] <= pd.Timestamp(win[1]) and e['last'] >= pd.Timestamp(win[0])
                flag = '  <== 命中用户圈' if overlap else ''
                matched = matched or overlap
            print(f"  {e['first'].date()} ~ {e['last'].date()} (x{e['n']}) trig={r['trigger_kind']:<5} "
                  f"floor={r.get('shelf_line')} ceil={r.get('ceil_line')} 触={r.get('touch_count')}/"
                  f"{r.get('ceil_touch_count')} 带=[{r.get('zone_lo')},{r.get('zone_hi')}] vp={r.get('vp_mid')} "
                  f"overshoot={r.get('overshoot_atr')}ATR stop={r.get('stop')} score={r.get('score')}{flag}")
        if win:
            hits_total += 1 if matched else 0
            misses_total += 0 if matched else 1
            if not matched:
                print('  >>> 未命中 <<<')
                box = L2.find_lines(df, True)
                if box is None:
                    print('  诊断: find_lines=None')
                else:
                    f, c = box['floors'][0], (box['ceil_options'][0] if box['ceil_options'] else None)
                    print(f"  诊断: floor m={f['m']:+.3f} touches={[str(df.iloc[i]['Date'].date()) for i, _ in f['touches'][:8]]}")
                    if c:
                        print(f"          ceil  m={c['m']:+.3f} touches={[str(df.iloc[i]['Date'].date()) for i, _ in c['touches'][:8]]}")
                    print(f"          trigger={L2.trigger_candidates(df, box, True)[:1]}")
    print(f'\n>>> 圈对齐: {hits_total} 命中 / {hits_total + misses_total} 个有标签例子；'
          f'（无法核对的标的不计入：存档窗口与截图不同期）')


if __name__ == '__main__':
    main()
