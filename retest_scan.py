"""A 版扫描器：破底翻已确认 → 监控回踩提醒
==========================================
分工（按用户 2026-11-03 的路线 A）：
  结构识别（双底/破底/翻回/见顶）用**原程序的百分比口径**，但修掉它挑错低点、
  强制两脚间隔≥20 根这两个缺陷；
  触发只负责后半段：见顶回调后，价格进入「下降趋势线 ∧ 0.618 回撤」重合区域就提醒
  （区域而非点，允许 overshoot，"很接近也提醒"）。

修掉的原程序缺陷：
  1) local_extrema 的 `idxs[-10:]` 截断 + 分形窗口 → 改用 find_pivots_uncapped（不截断）。
  2) MIN_DOUBLE_STRUCTURE_GAP>=20 → 双底两脚可以相邻（TSLA 03-14/04-05 只差 2 根），
     改为「两脚之间必须有一次高于两脚的低点（即真正的双底形状）」，不设最小间隔。
  3) 破底只取 lj+1 之后的**第一次**，且要求这是**最近一次底部结构**（原程序会去配 1 月的旧对）。
"""
from __future__ import annotations

import io
import numpy as np
import pandas as pd

import us_pattern_scan as base
import shelf_scan as v1
import line_scan as L2

# ---- 结构参数（沿用原程序口径）----
LOOKBACK = 170
PAIR_MAX_PCT = 0.045        # 原程序 3%，放宽到 4.5%（MRNA 多底价差较大）
BREAK_MIN_PCT = 0.005       # 原程序：破底至少 0.5%
BREAK_MAX_PCT = 0.10        # 原程序 8%，放宽到 10%
RECLAIM_BARS = 60           # 破底后多少根内必须翻回
RECENT_BOTTOM_BARS = 100    # 双底的右脚距今不得超过 N 根（保证是「最近那次」底部）
TOP_WINDOW = 5
TOP_MIN_DROP_ATR = 0.4     # 见顶后至少回落 N 个 ATR（原先按 3%% 固定百分比：MRNA 2.4%%、棉花 0.5%% 都被误杀）
# ---- 触发参数（区域 + overshoot）----
RECENT_TRIGGER = 8          # 触发 bar 必须在最新 N 根内
ZONE_ATR = 1.6              # 区域半宽（ATR 倍数）
NEAR_ZONE_ATR = 2.6         # 「很接近也提醒」：放宽档
FIB = 0.618
LONG_BOX_BARS = 60
BOX_MIN_OCCUPANCY = 0.34
BOX_MIN_WIDTH_PCT = 2.0
BOX_MAX_WIDTH_PCT = 13.0


def box_before_break(d: pd.DataFrame, brk_idx: int, lookback: int = 120):
    """破底之前的箱体：收盘价直方图上**连续**最密带，要求持续足够长且带宽合理。
    返回 {'lo','hi','occupancy','span','width_pct'} 或 None。"""
    a = max(0, brk_idx - lookback)
    seg = d.iloc[a:brk_idx]
    closes = seg['Close'].astype(float).to_numpy()
    if len(closes) < 40:
        return None
    lo, hi = float(np.nanmin(closes)), float(np.nanmax(closes))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return None
    nb = 20
    edges = np.linspace(lo, hi, nb + 1)
    w, _ = np.histogram(closes, bins=edges)
    w = w.astype(float)
    width = hi - lo
    max_bins = max(2, int(np.ceil(width * BOX_MAX_WIDTH_PCT / 100.0 / (edges[1] - edges[0]))))
    min_bins = max(1, int(np.floor(width * BOX_MIN_WIDTH_PCT / 100.0 / (edges[1] - edges[0]))))
    best_j, best_s = None, -1.0
    for n_bins in range(min_bins, min(max_bins, nb - 1) + 1):
        for j in range(0, nb - n_bins + 1):
            s = float(w[j:j + n_bins].sum())
            if s > best_s:
                best_s, best_j = s, (j, j + n_bins)
    if best_j is None:
        return None
    j0, j1 = best_j
    b_lo, b_hi = float(edges[j0]), float(edges[j1])
    inside = (closes >= b_lo) & (closes <= b_hi)
    occ = float(inside.mean())
    where = np.where(inside)[0]
    span = int(where[-1] - where[0] + 1) if len(where) else 0
    width_pct = (b_hi - b_lo) / max(b_lo, 1e-9) * 100.0
    if width_pct > BOX_MAX_WIDTH_PCT or width_pct < BOX_MIN_WIDTH_PCT:
        return None
    if occ < BOX_MIN_OCCUPANCY or span < 30:
        return None
    return {'lo': b_lo, 'hi': b_hi, 'occupancy': round(occ, 3), 'span': span,
            'width_pct': round(width_pct, 2)}



def pivot_lows(d: pd.DataFrame, i: int, lookback: int = LOOKBACK):
    """不截断的摆动低点（绝对索引）。"""
    lo_start = max(0, i - lookback)
    sub = d.iloc[lo_start:i + 1].reset_index(drop=True)
    _s, idxs = v1.find_pivots_uncapped(sub, 'low', len(sub), base.SWING_WINDOW)
    return [int(j) + lo_start for j in idxs]


def find_base(d: pd.DataFrame, i: int, atr: float):
    """最近那次底部结构：枚举所有达标配对，按「右脚+破底+翻回 越新」排序取最优。
    返回 {'feet','zone_low','zone_high','break_idx','reclaim_idx'} 或 None。

    注意：之前写成「最近的摆动低点必须当右脚，不行就返回 None」，结果 0/13 ——
    因为右脚不一定是最后一个分形低点（反弹后常出现略高的次要低点）。
    """
    closes = d['Close'].astype(float).to_numpy()
    lows = d['Low'].astype(float).to_numpy()
    piv = [j for j in pivot_lows(d, i) if j <= i]
    if len(piv) < 2:
        return None
    found = []
    for r in range(1, len(piv)):
        lj = piv[r]
        if i - lj > RECENT_BOTTOM_BARS:
            continue
        pl = float(lows[lj])
        for l in range(0, r):
            li = piv[l]
            p1 = float(lows[li])
            if base.pct_diff(p1, pl) > PAIR_MAX_PCT:
                continue
            if lj - li < 2:
                continue
            mid = lows[li + 1:lj]
            if len(mid) == 0 or float(np.nanmin(mid)) < min(p1, pl) - 0.8 * atr:
                continue      # 中间有更低的价 = 不是双底形状（允许 0.8 ATR 的假摔）
            zl, zh = min(p1, pl), max(p1, pl)
            bidx = None
            for k in range(lj + 1, i + 1):
                bp = min(float(lows[k]), float(closes[k]))
                mag = (zl - bp) / zl
                if BREAK_MIN_PCT <= mag <= BREAK_MAX_PCT:
                    bidx = k
                    break
            if bidx is None:
                continue
            cidx = None
            for k in range(bidx + 1, i + 1):
                if float(closes[k]) >= zh:
                    cidx = k
                    break
            if cidx is None or cidx - bidx > RECLAIM_BARS:
                continue
            found.append({'feet': [(li, p1), (lj, pl)], 'zone_low': zl, 'zone_high': zh,
                          'break_idx': bidx, 'reclaim_idx': cidx})
    if not found:
        return None
    # 新鲜度：翻回点最近者优先，其次破底最近
    found.sort(key=lambda x: (x['reclaim_idx'], x['break_idx'], x['feet'][1][0]), reverse=True)
    return found[0]


def stage_top(d: pd.DataFrame, after: int, i: int, atr: float, floor_idx: int = 0):
    """「阶段性见顶」= 破前高之后到今天的最高价，且其后确有回落 >= TOP_MIN_DROP_ATR。

    改掉的缺陷：原先要求它是个已走出右侧的局部高点，但用户的 SIRI 例子中
    见顶就是 2026-02-05（恰好是打破前高和趋势线那一天），右侧当时还没走出来 → 判不出。
    现在直接取推进段的最高点：top = argmax(High[after+1 .. i])，
    回落要求 = 该高点 − 其后的最低价 >= TOP_MIN_DROP_ATR × ATR。
    floor_idx：见顶下界（供 watch_long 传「翻回日」，默认 0 不改变原行为）。
    """
    highs = d['High'].astype(float).to_numpy()
    if after + 1 > i:
        return None
    seg = highs[after + 1:i + 1]
    if len(seg) == 0:
        return None
    j = int(after + 1 + int(np.nanargmax(seg)))
    if j >= i:                      # 最高点就是今天，还没开始回落
        return None
    after_h = highs[j + 1:i + 1]
    if len(after_h) == 0:
        return None
    if (float(highs[j]) - float(np.nanmin(after_h))) / atr < TOP_MIN_DROP_ATR:
        return None
    return j


def descending_channel(d: pd.DataFrame, i: int, atr: float):
    """下降趋势线：窗口最高高点 + 更低的高点；在 i 处贴近现价。返回含绝对索引 line_abs。"""
    lo_start = max(0, i - LOOKBACK)
    sub = d.iloc[lo_start:i + 1].reset_index(drop=True)
    n = len(sub)
    hi = sub['High'].astype(float).to_numpy()
    hp, _lp, _ = L2.leg_points(sub, n, atr)
    if not hp:
        return None
    top_i, top_p = max(hp, key=lambda x: x[1])
    last_close = float(sub['Close'].iloc[n - 1])
    best = None
    for (j, p) in hp:
        if j <= top_i or p >= top_p:
            continue
        m = (p - top_p) / (j - top_i)
        if m > -0.01 * atr:
            continue
        line = top_p + m * (np.arange(n) - top_i)
        if abs(float(line[n - 1]) - last_close) > 5.0 * atr:
            continue
        touches = sum(1 for (jj, pp) in hp if abs(pp - line[jj]) <= 1.3 * atr)
        if best is None or touches > best['touches']:
            best = {'top_i': top_i + lo_start, 'top_p': top_p, 'm': m,
                    'line_now': float(line[n - 1]), 'touches': touches}
    if best is None:
        return None
    full = np.empty(i + 1)
    full[lo_start:i + 1] = best['top_p'] + best['m'] * (np.arange(lo_start, i + 1) - best['top_i'])
    full[:lo_start] = full[lo_start]
    best['line_abs'] = full
    return best


def prior_break_exists(d: pd.DataFrame, base_: dict, ch, i: int):
    """翻回之后是否已「打破前高和趋势线」：收盘升破通道 或 升破此前显著高点。"""
    closes = d['Close'].astype(float).to_numpy()
    highs = d['High'].astype(float).to_numpy()
    r = base_['reclaim_idx']
    if ch is not None:
        above = [j for j in range(r + 1, i + 1) if closes[j] > float(ch['line_abs'][j])]
        if above:
            return above[0]
    win_hi = float(np.nanmax(highs[max(0, base_['feet'][-1][0] - LOOKBACK):base_['feet'][-1][0] + 1]))
    above = [j for j in range(r + 1, i + 1) if closes[j] > win_hi * (1 - 0.005)]
    return above[0] if above else None


def scan_retest_long(symbol: str, df: pd.DataFrame) -> dict | None:
    d = df.dropna(subset=['Open', 'High', 'Low', 'Close']).reset_index(drop=True)
    if len(d) < 120:
        return None
    d = d.copy()
    n = len(d); i = n - 1
    atr = v1.atr_of(d, 20)
    if not atr or atr <= 0:
        return None
    bs = find_base(d, i, atr)
    if not bs:
        return None
    ch = descending_channel(d, i, atr)
    pb = prior_break_exists(d, bs, ch, i)
    if pb is None:
        return None
    top = stage_top(d, max(pb, bs['reclaim_idx']), i, atr)
    if top is None:
        return None
    leg_high = float(d['High'].iloc[top])
    leg_low = float(min(bs['zone_low'], float(np.nanmin(d['Low'].iloc[bs['break_idx']:top + 1]))))
    if leg_high <= leg_low:
        return None
    fib618 = leg_high - (leg_high - leg_low) * FIB
    zones = [('fib', fib618)]
    if ch:
        zones.append(('trendline', float(ch['line_now'])))
    hit = None
    for k in range(i, max(top + 1, i - RECENT_TRIGGER) - 1, -1):
        lv = float(d['Low'].iloc[k])
        best_near = None
        for kind, z in zones:
            dist = abs(lv - z) / atr
            if dist <= ZONE_ATR:
                best_near = {'k': k, 'zone': z, 'kind': kind, 'dist': dist, 'tight': True}
                break
            if dist <= NEAR_ZONE_ATR and (best_near is None or dist < best_near['dist']):
                best_near = {'k': k, 'zone': z, 'kind': kind, 'dist': dist, 'tight': False}
        if best_near:
            hit = best_near
            break
    if not hit:
        return None
    pre = None
    if hasattr(v1, 'find_chip_density_window'):
        try:
            pre = v1.find_chip_density_window(d, bs['break_idx'])
        except Exception:
            pre = None
    long_box = bool(pre and pre.get('span_bars', 0) >= LONG_BOX_BARS)
    D = d['Date']
    score = 50.0
    score += 14.0 if hit['tight'] else 4.0
    score += 10.0 if hit['kind'] == 'fib' else 5.0
    if ch:
        score += 4.0 + min(8.0, ch['touches'] * 1.5)
    score += 10.0 if long_box else 0.0
    score += max(0.0, 8.0 - (i - hit['k']) * 1.2)
    stop = float(min(bs['zone_low'], np.nanmin(d['Low'].iloc[bs['break_idx']:i + 1])))
    rng = float(d['High'].iloc[hit['k']]) - stop
    return {'symbol': symbol, 'pattern': '破底翻回踩提醒',
            'trigger_date': str(D.iloc[hit['k']].date()), 'trigger_idx': int(hit['k']),
            'zone': round(hit['zone'], 4), 'zone_kind': hit['kind'],
            'tight': hit['tight'], 'dist_atr': round(hit['dist'], 2),
            'bottom_left': str(D.iloc[bs['feet'][0][0]].date()),
            'bottom_right': str(D.iloc[bs['feet'][1][0]].date()),
            'zone_low': round(bs['zone_low'], 4), 'zone_high': round(bs['zone_high'], 4),
            'break_date': str(D.iloc[bs['break_idx']].date()),
            'reclaim_date': str(D.iloc[bs['reclaim_idx']].date()),
            'prior_break_date': str(D.iloc[pb].date()),
            'stage_top_date': str(D.iloc[top].date()), 'stage_top': round(leg_high, 4),
            'fib618': round(fib618, 4),
            'trendline_now': None if not ch else round(float(ch['line_now']), 4),
            'trendline_top_date': None if not ch else str(D.iloc[ch['top_i']].date()),
            'atr': round(atr, 4), 'long_box_before_flip': long_box,
            'stop': round(stop, 4),
            'target_618': round(float(d['Close'].iloc[i]) + 0.618 * rng, 4),
            'target_full': round(float(d['Close'].iloc[i]) + rng, 4),
            'last_close': round(float(d['Close'].iloc[i]), 4), 'score': round(score, 1)}


# ---- 与现有 run_scan.sh / Actions 管线对接的接口（与 shelf_scan 同名）----
base_ = base
v1mod = v1


def scan_shelf_dataset(stage2: dict, mapped: dict, stderr_path: str):
    """与 run_scan.sh 管线的对接接口（签名/返回值与 us_pattern_scan 一致）。"""
    import traceback
    results, long_n, short_n = [], 0, 0
    for key, rows in stage2.items():
        sym = mapped.get(key, key) if mapped else key
        try:
            df = pd.DataFrame(rows)
            r = scan_retest_long(sym, df)
            if r:
                r.setdefault('direction', 'long')
                r['symbol'] = sym
                results.append(r)
                long_n += 1
        except Exception as exc:
            with io.open(stderr_path, 'a', encoding='utf-8') as f:
                f.write('%s: %s\n%s\n' % (sym, exc, traceback.format_exc()))
    return results, long_n, short_n


# 兼容旧名字
scan_retest_dataset = scan_shelf_dataset


DIAG_FIELDS = ['symbol', 'direction', 'pattern', 'trigger_date', 'zone_kind', 'zone',
               'dist_atr', 'tight', 'bottom_left', 'bottom_right', 'zone_low', 'zone_high',
               'break_date', 'reclaim_date', 'prior_break_date', 'stage_top_date', 'stage_top',
               'fib618', 'trendline_now', 'long_box_before_flip', 'atr', 'stop',
               'target_618', 'target_full', 'last_close', 'score']


def write_diag_csv(rows, path):
    import csv
    with io.open(str(path), 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=DIAG_FIELDS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def render_shelf_chart(rows_or_df, row, png_path):
    """把候选画成 PNG（matplotlib，标题用 ASCII：Actions 上没有中文字体）。"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import os
        df = rows_or_df
        if hasattr(df, 'empty'):
            x = range(len(df))
            fig, ax = plt.subplots(figsize=(11, 5.5))
            ax.plot(x, df['Close'].to_numpy(), lw=1.1, color='#222')
            hi = df['High'].to_numpy(); lo = df['Low'].to_numpy()
            ax.fill_between(x, lo, hi, color='#bbb', alpha=0.45)
            def hline(val, color, label):
                if val is None:
                    return
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return
                ax.axhline(v, color=color, ls='--', lw=1.0, label=label)
            hline(row.get('fib618'), '#e07b00', 'fib 0.618')
            hline(row.get('trendline_now'), '#7b0000', 'trendline(now)')
            hline(row.get('zone'), '#00703c', 'retest zone')
            hline(row.get('zone_low'), '#0050a0', 'base zone low')
            hline(row.get('zone_high'), '#0050a0', 'base zone high')
            hline(row.get('stop'), '#666666', 'stop')
            ax.legend(fontsize=7, loc='upper left')
            ax.set_title('%s retest score=%s' % (row.get('symbol'), row.get('score')),
                       fontsize=10)
            ax.set_xticks([])
            fig.tight_layout()
            fig.savefig(str(png_path), dpi=110)
            plt.close(fig)
            return os.path.exists(str(png_path))
    except Exception:
        return False
    return False

def watch_long(symbol: str, df: pd.DataFrame) -> dict | None:
    """观察名单条目：只要到今天就一条破底翻链成立，就输出事实，不要求价格进入任何区域。"""
    d = df.dropna(subset=['Open', 'High', 'Low', 'Close']).reset_index(drop=True)
    if len(d) < 120:
        return None
    d = d.copy()
    if 'Volume' not in d.columns:
        d['Volume'] = 0.0
    d['Volume'] = d['Volume'].fillna(0)
    n = len(d); i = n - 1
    atr = v1.atr_of(d, 20)
    if not atr or atr <= 0:
        return None
    bs = find_base(d, i, atr)
    if not bs:
        return None
    ch = descending_channel(d, i, atr)
    pb = prior_break_exists(d, bs, ch, i)
    if pb is None:
        return None
    top = stage_top(d, max(pb, bs['reclaim_idx']), i, atr, floor_idx=bs['reclaim_idx'])
    if top is None:
        return None
    lows_np = d['Low'].astype(float).to_numpy()
    closes_np = d['Close'].astype(float).to_numpy()
    highs_np = d['High'].astype(float).to_numpy()
    leg_high = float(highs_np[top])
    # 两种 0.618 锚：箱体/底部区上沿（深）与被打破处（浅）
    deep_leg = float(bs['zone_high'])
    broken_level = float(closes_np[pb])
    if ch:
        broken_level = max(broken_level, float(ch['line_abs'][pb]))
    fibs = {}
    if leg_high > deep_leg:
        fibs['deep'] = leg_high - (leg_high - deep_leg) * FIB
    if leg_high > broken_level:
        fibs['shallow'] = leg_high - (leg_high - broken_level) * FIB
    lv = float(lows_np[i])
    cl = float(closes_np[i])
    D = d['Date']
    # 是否已失效：收盘跌破底部区上沿（不判 3 根规则，只报事实）
    broken_base = cl < bs['zone_high']
    # 见顶后是否曾深破 0.618 且未收回：只报告，不做淘汰
    deep_fib = fibs.get('deep')
    pierce = None
    if deep_fib:
        ks = [k for k in range(top + 1, i + 1) if lows_np[k] < deep_fib - 1.5 * atr]
        if ks:
            k = ks[-1]
            win = closes_np[k + 1:k + 4]
            pierce = {'date': str(D.iloc[k].date()),
                      'recovered': bool(len(win) and float(np.nanmax(win)) >= deep_fib)}
    box = None
    if 'box_before_break' in globals():
        try:
            box = box_before_break(d, bs['break_idx'])
        except Exception:
            box = None
    return {'symbol': symbol, 'trigger_date': str(D.iloc[i].date()),
            'bottom_left_date': str(D.iloc[bs['feet'][0][0]].date()),
            'bottom_right_date': str(D.iloc[bs['feet'][1][0]].date()),
            'base_low': round(bs['zone_low'], 4), 'base_high': round(bs['zone_high'], 4),
            'break_date': str(D.iloc[bs['break_idx']].date()),
            'reclaim_date': str(D.iloc[bs['reclaim_idx']].date()),
            'prior_break_date': str(D.iloc[pb].date()),
            'stage_top_date': str(D.iloc[top].date()), 'stage_top': round(leg_high, 4),
            'bars_since_top': int(i - top), 'fib_deep': None if 'deep' not in fibs else round(fibs['deep'], 4),
            'fib_shallow': None if 'shallow' not in fibs else round(fibs['shallow'], 4),
            'trendline_now': None if not ch else round(float(ch['line_now']), 4),
            'trendline_top_date': None if not ch else str(D.iloc[ch['top_i']].date()),
            'today_low': round(lv, 4), 'today_close': round(cl, 4), 'atr': round(atr, 4),
            'dist_fib_deep': None if 'deep' not in fibs else round((lv - fibs['deep']) / atr, 2),
            'dist_fib_shallow': None if 'shallow' not in fibs else round((lv - fibs['shallow']) / atr, 2),
            'dist_trendline': None if not ch else round((lv - float(ch['line_now'])) / atr, 2),
            'long_box_before_flip': bool(box and box.get('span', 0) >= LONG_BOX_BARS),
            'box_span': None if not box else box.get('span'),
            'base_broken_close': broken_base,
            'deep_fib_pierced': None if pierce is None else pierce['date'],
            'deep_fib_recovered': None if pierce is None else pierce['recovered'],
            'note': '仅陈述事实：是否进入回踩区/该不该进由你判断'}


WATCH_FIELDS = ['symbol', 'trigger_date', 'stage_top_date', 'bars_since_top', 'stage_top',
                'fib_deep', 'fib_shallow', 'trendline_now', 'today_low', 'today_close', 'atr',
                'dist_fib_deep', 'dist_fib_shallow', 'dist_trendline',
                'bottom_left_date', 'bottom_right_date', 'base_low', 'base_high',
                'break_date', 'reclaim_date', 'prior_break_date',
                'long_box_before_flip', 'box_span', 'base_broken_close',
                'deep_fib_pierced', 'deep_fib_recovered', 'note']


def watch_dataset(stage2: dict, mapped: dict, stderr_path: str):
    import io as _io
    import traceback
    rows, err = [], 0
    for key, recs in stage2.items():
        sym = mapped.get(key, key) if mapped else key
        try:
            r = watch_long(sym, pd.DataFrame(recs))
            if r:
                rows.append(r)
        except Exception as exc:
            err += 1
            with _io.open(stderr_path, 'a', encoding='utf-8') as f:
                f.write('%s: %s\n%s\n' % (sym, exc, traceback.format_exc()))
    return rows, err


def write_watch_csv(rows, path):
    import csv
    import io as _io
    with _io.open(str(path), 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=WATCH_FIELDS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)

