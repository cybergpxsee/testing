"""误报摸底：把 retest_scan 在每个历史 bar 上当"今天"跑一遍，统计触发频率。
13/13 只说明"圈上会报"，这个测的是"不在圈上会不会也乱报"。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
sys.path.insert(0, str(Path('examples').resolve()))
import pandas as pd
import retest_scan as A
from parse_yahoo_html import load_all

CIRCLES = {}
for line in (Path('examples') / 'structures.csv').read_text(encoding='utf-8-sig').splitlines():
    p = [x.strip() for x in line.split(',')]
    if len(p) >= 5 and p[2] == 'entry':
        CIRCLES.setdefault(p[0].upper(), set()).add(pd.Timestamp(p[3]))

data = load_all()
tot_fire = tot_bars = 0
for sym, df in sorted(data.items()):
    if sym not in CIRCLES:
        continue
    df = df.reset_index(drop=True)
    fires = []
    for end in range(120, len(df)):
        r = A.scan_retest_long(sym, df.iloc[:end + 1].reset_index(drop=True))
        if r:
            fires.append(pd.Timestamp(r['trigger_date']))
    n = len(df) - 120
    # 合并成事件（相邻 bar 只算一次）
    evs = []
    for t in fires:
        if evs and (t - evs[-1][-1]).days <= 7:
            evs[-1].append(t)
        else:
            evs.append([t])
    circ = CIRCLES[sym]
    hit = sum(1 for e in evs if any(c in circ and abs((c - e[-1]).days) <= 7 for c in circ) or
              any(c in e and abs((c - e[-1]).days) <= 10 for c in circ))
    tot_fire += len(evs); tot_bars += n
    print('%-10s 窗口%4d根 触发%4d bar → %2d 个事件 | 覆盖你的圈 %d/%d | 其余 %d 个事件不在你的圈上'
          % (sym, n, len(fires), len(evs), hit, len(circ), len(evs) - hit))
    for e in evs:
        tag = '★含你的圈' if any(c in circ and (e[0] - c).days <= 10 and (c - e[-1]).days <= 3 for c in circ) else ''
        print('      %s ~ %s (%d bar) %s' % (e[0].date(), e[-1].date(), len(e), tag))
print('\n>>>>> 合计 %d 个事件 / %d 根窗口 ≈ 每 %d 根一次；你的圈共 %d 个'
      % (tot_fire, tot_bars, max(1, tot_bars // max(1, tot_fire)), sum(len(v) for v in CIRCLES.values())))
