#!/usr/bin/env python3
"""
Generate Discord embed payload for consolidation scan results
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone


def build_field_value(rows, title):
    if not rows:
        return "```text\n  --  无  --\n```"
    lines = ["```text"]
    lines.append(f" {title} (Top {len(rows[:10])})")
    lines.append(" 序  代码    盘整周  破底翻  评分")
    for idx, row in enumerate(rows[:10], 1):
        symbol = row.get('symbol', '')
        weeks = row.get('_duration_weeks', 0)
        rev = row.get('reversal_count', 0)
        scr = row.get('score', 0)
        lines.append(f"{idx:2d}. {symbol:<6} {weeks:3d}周   {rev:2d}次   {scr:5.1f}")
    lines.append("```")
    return '\n'.join(lines)


def create_discord_payload(scan_result):
    generated_at = scan_result.get('generated_at_utc', datetime.now(timezone.utc).isoformat())
    scan_date = generated_at[:10]
    cons = scan_result.get('top10_consolidating', [])
    break_out = scan_result.get('top10_breaking_out', [])
    universe = scan_result.get('universe_total', 'N/A')
    liquid = scan_result.get('liquid_count', 'N/A')
    deep = scan_result.get('deep_scan_count', 'N/A')
    
    def build_field_value(rows, title):
        if not rows:
            return "```text\n  --  无  --\n```"
        lines = ["```text"]
        lines.append(f" {title} (Top {len(rows[:10])})")
        lines.append(" 序  代码    盘整周  破底翻  评分")
        for idx, row in enumerate(rows[:10], 1):
            symbol = row.get('symbol', '')
            weeks = row.get('_duration_weeks', 0)
            rev = row.get('reversal_count', 0)
            scr = row.get('score', 0)
            lines.append(f"{idx:2d}. {symbol:<6} {weeks:3d}周   {rev:2d}次   {scr:5.1f}")
        lines.append("```")
        return '\n'.join(lines)

    embed = {
        "title": "📊 长期底部盘整扫描",
        "description": f"**日期**: {scan_date}\n📊 扫描 {universe} 支 → 流动性 {liquid} 支 → 深度扫 {deep} 支\n🟡 盘整中 {len(cons)} ｜ 🟢 刚突破 {len(break_out)}",
        "color": 0x2B2D42,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "周线数据 3年 | 仅供研究参考"},
        "fields": [
            {"name": f"🟡 盘整中（前10）", "value": build_field_value(cons, "盘整中"), "inline": False},
            {"name": f"🟢 刚突破（前10）", "value": build_field_value(break_out, "刚突破"), "inline": False},
        ]
    }
    return {"embeds": [embed], "username": "Consolidation Scanner"}


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: render_discord.py <scan_result.json> <output_dir>")
        sys.exit(1)
    scan_result = json.load(open(sys.argv[1]))
    payload = create_discord_payload(scan_result)
    out_path = Path(sys.argv[2]) / f"discord_embed_{datetime.now().strftime('%Y%m%d')}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Discord payload saved: {out_path}")