#!/usr/bin/env python3
"""
Render consolidation scan results to Markdown report
"""
import json
import sys
from pathlib import Path


def fmt_mmdd(date_str: str) -> str:
    s = str(date_str)
    return s[5:10] if len(s) >= 10 else s


def build_table(rows, title):
    if not rows:
        return f"**{title}**：无\n"
    table = f"**{title}**\n"
    table += "| 序 | 代码 | 区间 | 盘整周数 | 破底翻 | 现价 | 评分 | 逻辑 |\n"
    table += "|---|---|---|---|---|---:|---:|---|\n"
    for idx, row in enumerate(rows[:10], 1):
        symbol = row.get('symbol', '')
        zone = row.get('zone', '')
        weeks = row.get('_duration_weeks', 0)
        rev = row.get('reversal_count', 0)
        price = row.get('price', 0)
        scr = row.get('score', 0)
        logic = row.get('logic', '')
        table += f"| {idx} | {symbol} | {zone} | {weeks} | {rev} | {price:.2f} | {scr:.1f} | {logic} |\n"
    return table + "\n"


def render(out: dict) -> str:
    lines = []
    lines.append("📊 美股长期底部盘整扫描报告")
    lines.append("")

    generated_at = str(out.get('generated_at_utc', ''))
    report_date = generated_at[5:10] if len(generated_at) >= 10 else '未知'
    data_sources = out.get('data_sources') or ['Nasdaq Trader 月更股票池快取', 'Yahoo Finance / yfinance 周线 OHLCV']
    data_sources = [str(x).replace('日线', '周线') for x in data_sources]

    cons_list = out.get('top10_consolidating', [])
    break_list = out.get('top10_breaking_out', [])

    lines.append(f"🗂️ 数据来源：{'；'.join(data_sources)}")
    lines.append(f'📅 报告日期：{report_date}')
    lines.append("")

    miss_total = int(out.get('stage1_misses', 0)) + int(out.get('stage2_misses', 0))
    miss_note = f"；数据下载失败 {miss_total} 个" if miss_total else ""
    lines.append(
        f"摘要：共扫描 {out.get('universe_total', 0)} 个标的，"
        f"通过流动性过滤 {out.get('liquid_count', 0)} 个，"
        f"深度扫描 {out.get('deep_scan_count', 0)} 个，"
        f"底部盘整 {out.get('consolidating_count', 0)} 个，"
        f"刚突破 {out.get('breaking_out_count', 0)} 个，"
        f"最终输出前 {len(out.get('top10_consolidating', [])) + len(out.get('top10_breaking_out', []))} 个{miss_note}。"
    )
    lines.append("")

    def build_table(rows, title):
        if not rows:
            return f"**{title}**：无\n"
        table = f"**{title}**\n"
        table += "| 序 | 代码 | 区间 | 盘整周数 | 破底翻 | 现价 | 评分 | 逻辑 |\n"
        table += "|---|---|---|---|---|---:|---:|---|\n"
        for idx, row in enumerate(rows[:10], 1):
            symbol = row.get('symbol', '')
            zone = row.get('zone', '')
            weeks = row.get('_duration_weeks', 0)
            rev = row.get('reversal_count', 0)
            price = row.get('price', 0)
            scr = row.get('score', 0)
            logic = row.get('logic', '')
            table += f"| {idx} | {symbol} | {zone} | {weeks} | {rev} | {price:.2f} | {scr:.1f} | {logic} |\n"
        return table + "\n"

    lines.append('## 🟡 盘整中（前10）')
    lines.append(build_table(cons_list, '底部盘整中'))
    lines.append('## 🟢 刚突破（前10）')
    lines.append(build_table(break_list, '底部突破'))

    lines.append('⚠️ 风险提示：此为AI扫描结果，仅供参考，不构成投资建议。')
    return '\n'.join(lines)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: render_report.py input.json output.md')
    json_path = Path(sys.argv[1])
    md_path = Path(sys.argv[2])
    out = json.loads(json_path.read_text(encoding='utf-8'))
    text = render(out)
    md_path.write_text(text, encoding='utf-8')
    print(text)