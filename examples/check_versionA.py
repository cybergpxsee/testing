"""核对 A 版：事件日期 vs 用户口述 + 圈是否触发。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import retest_scan as A
from parse_yahoo_html import load_all

TRUE = {
    'HOOD':      {'bl': '2026-02-24', 'br': '2026-03-24', 'break': '2026-03-27', 'reclaim': '2026-04-08',
                  'prior': '2026-04-14', 'top': '2026-04-17',
                  'circles': ['2026-04-29', '2026-04-30', '2026-05-01']},
    'TSLA':      {'bl': '2024-03-14', 'br': '2024-04-05', 'break': '2024-04-16', 'reclaim': '2024-04-25',
                  'prior': '2024-04-29', 'top': '2024-05-06', 'circles': ['2024-05-10']},
    'MRNA':      {'br': '2025-11-17', 'break': '2025-11-20', 'reclaim': '2025-11-21',
                  'prior': '2025-11-26', 'top': '2025-11-28',
                  'circles': ['2025-12-01', '2025-12-02', '2025-12-03']},
    'SIRI':      {'bl': '2025-12-31', 'br': '2026-01-20', 'break': '2026-02-03', 'reclaim': '2026-02-04',
                  'prior': '2026-02-05', 'top': '2026-02-12',
                  'circles': ['2026-02-09', '2026-02-17', '2026-02-25']},
    'CTK28.NYB': {'circles': ['2026-03-03', '2026-03-05']},
    'BTC-USD':   {'circles': ['2023-03-10']},
}
TOL = pd.Timedelta(days=8)
data = load_all()
cnt = {'ev_ok': 0, 'ev_tot': 0, 'ci_ok': 0, 'ci_tot': 0}
for sym, t in TRUE.items():
    if sym not in data:
        print('\n!! %s 无数据' % sym); continue
    df = data[sym].reset_index(drop=True)
    print('\n===== %s =====' % sym)
    for cd in t['circles']:
        if not (df.Date == pd.Timestamp(cd)).any():
            print('  圈 %s 不在窗口' % cd); cnt['ci_tot'] += 1; continue
        pos = int(df.index[df.Date == pd.Timestamp(cd)][0])
        dg = {}
        r = A.scan_retest_long(sym, df.iloc[:pos + 1].reset_index(drop=True))
        cnt['ci_tot'] += 1
        if not r:
            # 逐环节定位
            import numpy as np, shelf_scan as v1
            dd = df.iloc[:pos + 1].reset_index(drop=True); ii = len(dd) - 1
            atr2 = v1.atr_of(dd, 20)
            bs = A.find_base(dd, ii, atr2)
            steps = []
            if not bs: steps.append('找不到达标双底/破底/翻回')
            else:
                ch2 = A.descending_channel(dd, ii, atr2)
                pb2 = A.prior_break_exists(dd, bs, ch2, ii)
                steps.append('双底 %s/%s 区%.2f~%.2f 破底 %s 翻回 %s' % (
                    dd.Date.iloc[bs['feet'][0][0]].date(), dd.Date.iloc[bs['feet'][1][0]].date(),
                    bs['zone_low'], bs['zone_high'], dd.Date.iloc[bs['break_idx']].date(), dd.Date.iloc[bs['reclaim_idx']].date()))
                if pb2 is None: steps.append('未见打破前高/趋势线')
                else:
                    tp = A.stage_top(dd, max(pb2, bs['reclaim_idx']), ii, atr2)
                    if tp is None:
                        import numpy as _np
                        hh = dd.High.astype(float).to_numpy()
                        lo0 = max(pb2, bs['reclaim_idx'])
                        cands = [j for j in range(lo0 + 1, ii)
                                 if j + 1 <= ii and hh[j] > hh[j + 1:min(ii + 1, j + A.TOP_WINDOW + 1)].max(initial=-1)]
                        det = []
                        for j in cands[-4:]:
                            ah = hh[j + 1:ii + 1]
                            det.append('%s 回落%.1f%%' % (dd.Date.iloc[j].date(),
                                                         (hh[j] - ah.min()) / hh[j] * 100 if len(ah) else 0.0))
                        steps.append('未见见顶: 近端候选 %s 需回落>=%.1f ATR'
                                     % (' '.join(det) or '无', A.TOP_MIN_DROP_ATR))
                    else:
                        lh = float(dd.High.iloc[tp]); ll = min(bs['zone_low'], float(np.nanmin(dd.Low.iloc[bs['break_idx']:tp+1])))
                        fib = lh - (lh - ll) * A.FIB
                        tl = float(ch2['line_now']) if ch2 else None
                        step = ii - max(tp + 1, ii - A.RECENT_TRIGGER) + 1
                        dists = []
                        for k in range(ii, max(tp + 1, ii - A.RECENT_TRIGGER) - 1, -1):
                            lv = float(dd.Low.iloc[k]); dists.append('%s:%.2fATR' % (dd.Date.iloc[k].date(), abs(lv - fib) / atr2))
                        steps.append('见顶 %s fib618=%.2f 趋势线=%s 近端Low距fib: %s (容差%.1f/%.1f)'
                                     % (dd.Date.iloc[tp].date(), fib, None if tl is None else round(tl, 2),
                                        ' '.join(dists[:6]), A.ZONE_ATR, A.NEAR_ZONE_ATR))
            print('  圈 %s  ❌ | %s' % (cd, ' || '.join(steps))); continue
        cnt['ci_ok'] += 1
        print('  圈 %s  ✅ 触发=%s %s区=%.2f 距离=%.2fATR%s 见顶=%s 破前高=%s 翻回=%s 破底=%s 双底=%s/%s fib618=%s 趋势线=%s score=%s'
              % (cd, r['trigger_date'], r['zone_kind'], r['zone'], r['dist_atr'],
                 '' if r['tight'] else '(很近)', r['stage_top_date'], r['prior_break_date'],
                 r['reclaim_date'], r['break_date'], r['bottom_left'], r['bottom_right'],
                 r['fib618'], r['trendline_now'], r['score']))
        if cd == t['circles'][-1]:   # 事件比对只在最后一个圈做一次
            for role, key in (('双底左','bottom_left'), ('双底右','bottom_right'), ('破底','break_date'),
                              ('翻回','reclaim_date'), ('破前高','prior_break_date'), ('见顶','stage_top_date')):
                tv = t.get(role)
                if not tv:
                    continue
                cnt['ev_tot'] += 1
                ok = abs(pd.Timestamp(r[key]) - pd.Timestamp(tv)) <= TOL
                cnt['ev_ok'] += ok
                print('     %-6s 用户 %s → A版 %s %s' % (role, tv, r[key], '✔' if ok else '✘'))
print('\n>>>>> A版: 圈触发 %d/%d   事件日期对齐 %d/%d'
      % (cnt['ci_ok'], cnt['ci_tot'], cnt['ev_ok'], cnt['ev_tot']))
