"""
Rendering utilities for US Pullback Scanner.
Common table building, formatting, and rendering functions.
"""
from typing import List, Dict, Any, Optional
import pandas as pd


def build_markdown_table(
    headers: List[str],
    rows: List[List[Any]],
    alignments: Optional[List[str]] = None
) -> str:
    """
    Build a markdown table from headers and rows.
    
    Args:
        headers: Column headers
        rows: List of row data
        alignments: Optional column alignments ('left', 'right', 'center')
        
    Returns:
        Markdown table string
    """
    if not headers:
        return ""
    
    lines = []
    
    # Header row
    lines.append("| " + " | ".join(str(h) for h in headers) + " |")
    
    # Separator row
    if alignments:
        sep = []
        for align in alignments:
            if align == 'left':
                sep.append(":---")
            elif align == 'right':
                sep.append("---:")
            elif align == 'center':
                sep.append(":---:")
            else:
                sep.append("---")
        lines.append("| " + " | ".join(sep) + " |")
    else:
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    
    # Data rows
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    
    return "\n".join(lines)


def build_code_block_table(
    headers: List[str],
    rows: List[List[Any]],
    alignments: Optional[List[str]] = None
) -> str:
    """
    Build a markdown table wrapped in code block for fixed-width rendering.
    
    Args:
        headers: Column headers
        rows: List of row data
        alignments: Optional column alignments
        
    Returns:
        Markdown code block with table
    """
    table = build_markdown_table(headers, rows, alignments)
    return f"```text\n{table}\n```"


def format_score(score: float, precision: int = 1) -> str:
    """Format score with fixed precision."""
    return f"{float(score):.{precision}f}"


def format_price(price: float, precision: int = 2) -> str:
    """Format price with fixed precision."""
    return f"{float(price):.{precision}f}"


def format_pct(value: float, precision: int = 2) -> str:
    """Format percentage value."""
    return f"{float(value):.{precision}f}%"


def sort_candidates(
    candidates: List[Dict],
    sort_keys: List[str] = None,
    reverse: bool = True
) -> List[Dict]:
    """
    Sort candidates by multiple keys.
    
    Args:
        candidates: List of candidate dicts
        sort_keys: List of keys to sort by (e.g., ['score', '_sort_pullback'])
        reverse: Whether to sort descending
        
    Returns:
        Sorted list
    """
    if sort_keys is None:
        sort_keys = ['score', '_sort_pullback', '_sort_event', '_sort_confirm']
    
    return sorted(
        candidates,
        key=lambda x: tuple(x.get(k, 0) for k in sort_keys),
        reverse=reverse
    )


def deduplicate_by_symbol(candidates: List[Dict]) -> List[Dict]:
    """Remove duplicate symbols, keeping highest scored."""
    seen = set()
    result = []
    for c in candidates:
        sym = c.get('symbol')
        if sym not in seen:
            seen.add(sym)
            result.append(c)
    return result


def filter_by_direction(candidates: List[Dict], direction: str) -> List[Dict]:
    """Filter candidates by direction (做多/做空)."""
    return [c for c in candidates if c.get('direction') == direction]


def get_liquidity_bands(candidates: List[Dict]) -> Dict[str, List[Dict]]:
    """Group candidates by liquidity band."""
    bands = {
        '20m_to_50m': [],
        '50m_plus': [],
    }
    for c in candidates:
        band = c.get('liquidity_band')
        if band in bands:
            bands[band].append(c)
    return bands


def format_pullback_dates(row: Dict) -> str:
    """Format pullback dates from recent_windows."""
    windows = row.get('recent_windows') or []
    if windows:
        return ' / '.join(w.get('representative_date', '') for w in windows if w.get('representative_date'))
    return row.get('pullback_date', '')


def build_report_sections(
    out: Dict,
    top10: List[Dict],
    headers: List[str],
    row_formatter
) -> List[str]:
    """
    Build report sections for markdown output.
    
    Args:
        out: Output dictionary with scan metadata
        top10: Top 10 candidates
        headers: Table headers
        row_formatter: Function to format each row
        
    Returns:
        List of markdown lines
    """
    lines = []
    
    # Header
    miss_total = int(out.get('stage1_misses', 0)) + int(out.get('stage2_misses', 0))
    miss_note = f"；数据下载失败 {miss_total} 个" if miss_total else ""
    lines.append(
        f"摘要：共扫描 {out.get('universe_total', 0)} 个标的，"
        f"通过流动性过滤 {out.get('liquid_count', 0)} 个，"
        f"深度扫描 {out.get('deep_scan_count', 0)} 个，"
        f"形成候选 {out.get('candidate_total', 0)} 个，"
        f"其中做多 {out.get('long_candidates', 0)} 个、做空 {out.get('short_candidates', 0)} 个，"
        f"最终输出前 {len(top10)} 个{miss_note}。"
    )
    lines.append("")
    
    if not top10:
        lines.append("今日无符合'确认后回调再介入'条件的标的。")
        if out.get('stderr_log'):
            lines.append("")
            lines.append(f"日志：`{out['stderr_log']}`")
        return lines
    
    # Table
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    
    for row in top10:
        lines.append("| " + " | ".join(row_formatter(row)) + " |")
    
    return lines


def add_observation_notes(lines: List[str], top10: List[Dict]):
    """Add observation notes to report."""
    lines.append("")
    lines.append("## 观察要点")
    lines.append("")
    
    long_top = [x for x in top10 if x['direction'] == '做多']
    short_top = [x for x in top10 if x['direction'] == '做空']
    qty_shrink = sum(1 for x in top10 if x['volume_feature'] == '量缩')
    qty_slow = sum(1 for x in top10 if '减速' in x['slowdown_feature'])
    newest = top10[0]
    
    lines.append(f"- 今日最优先关注的是最近回调/回抽日最新的标的：**{newest['symbol']}**（{newest['direction']} / {newest['pattern']}）。")
    lines.append(f"- 前10中量缩回踩/回抽共有 **{qty_shrink}** 个，说明不少候选属于缩量测试关键区的类型。")
    lines.append(f"- 前10中出现减速回调/减速回抽特征的共有 **{qty_slow}** 个，这类通常更接近理想二次介入结构。")
    lines.append(f"- 多头候选 **{len(long_top)}** 个，空头候选 **{len(short_top)}** 个，可用来判断当天偏风险偏好还是偏防守。")
    lines.append("- 支撑/阻力区、筹码密集区中轴、0.618 位置均为日线近似计算，适合做盘后筛选，不替代盘中确认。")
    lines.append("- 若次日出现放量重新站上支撑/跌回阻力下方，通常比单纯到位但未确认的胜率更高。")


def render_markdown_report(out: Dict) -> str:
    """Render full markdown report from output dict."""
    top10 = out.get('top10', []) or []
    
    headers = [
        "代码", "方向", "形态", "支撑/阻力区", "母形态事件日", "确认日",
        "最近回调/回抽日", "现价", "0.618关键位", "量能特征",
        "减速特征", "质量分", "一句话逻辑"
    ]
    
    def format_row(row):
        return [
            row['symbol'],
            row['direction'],
            row['pattern'],
            row['zone'],
            row['event_date'],
            row['confirm_date'],
            format_pullback_dates(row),
            format_price(row['price']),
            format_price(row['fib618']),
            row['volume_feature'],
            row['slowdown_feature'],
            format_score(row['score']),
            row['logic'],
        ]
    
    lines = build_report_sections(out, top10, headers, format_row)
    add_observation_notes(lines, top10)
    
    if out.get('stderr_log'):
        lines.append("")
        lines.append(f"日志：`{out['stderr_log']}`")
    
    return "\n".join(lines)