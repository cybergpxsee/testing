"""观察名单 · 本地/CI 运行入口
用法：
    python examples/monitor_run.py                  # 用 data/history/ 的 CSV + HTML 存档
    python examples/monitor_run.py --out watchlist.csv
输出：每行一个标的的「事实表」（结构要素日期 + 0.618(两种锚) + 趋势线 + 今日距离 + 箱体），
不做买卖判定。过滤：见顶后 25 根内 且 今日最低价距任一水平 2.5 ATR 内。
"""
import sys
import csv
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import retest_scan as A
from parse_yahoo_html import load_all

DIST_ATR = 2.5
MAX_BARS_FROM_TOP = 25


def show(r):
    ds = [r.get('dist_fib_deep'), r.get('dist_fib_shallow'), r.get('dist_trendline')]
    return (any(d is not None and abs(d) <= DIST_ATR for d in ds)
            and r.get('bars_since_top', 999) <= MAX_BARS_FROM_TOP)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='retest_watchlist.csv')
    args = ap.parse_args()
    data = load_all()
    rows = []
    for sym, df in sorted(data.items()):
        df = df.reset_index(drop=True)
        try:
            r = A.watch_long(sym, df)
        except Exception as exc:
            print('  !! %s: %s' % (sym, exc))
            continue
        if r and show(r):
            rows.append(r)
    if not rows:
        print('今天没有符合条件的条目（0 条）。')
        return 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=A.WATCH_FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print('%d 条已写入 %s' % (len(rows), args.out))
    for r in rows:
        print('  %-9s 见顶%s %d根前 | 今日低 %s 距fib深 %s / fib浅 %s / 线 %s | 双底 %s~%s 破底 %s 翻回 %s 破前高 %s | 长箱体=%s(%s根)'
              % (r['symbol'], r['stage_top_date'], r['bars_since_top'], r['today_low'],
                 r['dist_fib_deep'], r['dist_fib_shallow'], r['dist_trendline'],
                 r['bottom_left_date'], r['bottom_right_date'], r['break_date'], r['reclaim_date'],
                 r['prior_break_date'], r['long_box_before_flip'], r['box_span']))
    print('\n注意：这张表只陈述结构事实与距离，是否进场由你判断。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
