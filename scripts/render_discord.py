#!/usr/bin/env python3
"""
Discord Embed payload generator for pullback scan results.

Generates a rich Discord embed with:
- Title + metadata (scan date, data sources, candidate counts)
- Four grouped fields (Long 50M+, Long 20M-50M, Short 50M+, Short 20M-50M)
- Each field shows top 10 with Rank, Symbol, Pullback Date
- Footer with data source + risk warning
- Color-coded: Green for Long, Red for Short
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any


# Discord embed color constants
COLOR_LONG = 0x00FF7F   # Spring Green
COLOR_SHORT = 0xFF4444  # Red
COLOR_NEUTRAL = 0xFFAA00  # Orange (for main embed)


def fmt_mmdd(date_str: str) -> str:
    """Format YYYY-MM-DD to MM-DD."""
    s = str(date_str)
    return s[5:10] if len(s) >= 10 else s


def display_pullback_dates(row: Dict) -> str:
    """Get pullback date(s) from row - use recent_windows if available."""
    windows = row.get('recent_windows') or []
    if windows:
        dates = []
        seen = set()
        for w in windows:
            raw = w.get('representative_date', '')
            if not raw:
                continue
            mmdd = fmt_mmdd(raw)
            if mmdd and mmdd not in seen:
                dates.append(mmdd)
                seen.add(mmdd)
        if dates:
            return ' / '.join(dates)
    return fmt_mmdd(row.get('pullback_date', ''))


def build_group_lines(rows: List[Dict], direction: str, max_show: int = 10) -> str:
    """Build formatted lines for a group table."""
    if not rows:
        return "  --  無  --\n"

    lines = []
    # Header row - aligned with data: rank, symbol, date
    # Data format: `01.` SYMBOL    `DATE`
    # Symbol column starts after `01.` (4 chars) + 1 space = position 5
    lines.append("`編號  代碼    回調日`")
    for idx, row in enumerate(rows[:max_show], start=1):
        symbol = str(row.get('symbol', ''))
        pdate = display_pullback_dates(row)
        lines.append(f"`{idx:02d}.` {symbol:<6}  `{pdate}`")
    return '\n'.join(lines) + '\n'


def build_field_value(rows: List[Dict], direction: str) -> str:
    """Build Discord embed field value with code block."""
    content = build_group_lines(rows, direction)
    return f"```text\n{content}```"


def create_discord_payload(scan_result: Dict[str, Any]) -> Dict[str, Any]:
    """Create Discord webhook payload with embed."""

    # Extract metadata
    generated_at = scan_result.get('generated_at_utc', datetime.now(timezone.utc).isoformat())
    try:
        scan_date = generated_at[:10]
    except Exception:
        scan_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    universe_total = scan_result.get('universe_total', 'N/A')
    liquid_count = scan_result.get('liquid_count', 'N/A')
    deep_scan = scan_result.get('deep_scan_count', 'N/A')

    # Get grouped candidates
    long_50m = scan_result.get('top10_long_50m_plus', []) or []
    long_20m = scan_result.get('top10_long_20m_to_50m', []) or []
    short_50m = scan_result.get('top10_short_50m_plus', []) or []
    short_20m = scan_result.get('top10_short_20m_to_50m', []) or []

    total_long = len(long_50m) + len(long_20m)
    total_short = len(short_50m) + len(short_20m)
    total_candidates = total_long + total_short

    # Build main embed
    embed = {
        "title": "📊 美股回調交易形態掃描報告",
        "description": (
            f"**掃描日期**: {scan_date}\n"
            f"📊 **掃描範圍**: {universe_total} 支 → 流動性 {liquid_count} 支 → 深度掃描 {deep_scan} 支\n"
            f"🟢 **做多候選**: {total_long} ｜ 🔴 **做空候選**: {total_short}"
        ),
        "color": COLOR_NEUTRAL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": "Pullback Scanner v0.3 | 數據來源: Yahoo Finance (1y) | 僅供參考非投資建議"
        },
        "fields": []
    }

    # Add four groups as fields (inline=False for full width)
    groups = [
        ("🟢 做多・50M+ (高流通量)", long_50m, "做多", COLOR_LONG),
        ("🟢 做多・20M-50M (中流通量)", long_20m, "做多", COLOR_LONG),
        ("🔴 做空・50M+ (高流通量)", short_50m, "做空", COLOR_SHORT),
        ("🔴 做空・20M-50M (中流通量)", short_20m, "做空", COLOR_SHORT),
    ]

    for title, rows, direction, _ in groups:
        field_value = build_field_value(rows, direction)
        embed["fields"].append({
            "name": f"{title} ({len(rows)}檔)",
            "value": field_value,
            "inline": False
        })

    # Return payload for Discord webhook
    return {
        "embeds": [embed],
        "username": "Pullback Scanner",
        "avatar_url": "https://cdn.discordapp.com/emojis/123456789.png"
    }


def save_payload(scan_result_path: Path, output_dir: Path) -> Path:
    """Load scan result, generate payload, save to output_dir."""
    with open(scan_result_path, 'r', encoding='utf-8') as f:
        scan_result = json.load(f)

    payload = create_discord_payload(scan_result)

    scan_date = scan_result.get('generated_at_utc', datetime.now().strftime('%Y-%m-%d'))[:10]
    out_path = output_dir / f"discord_embed_{scan_date}_US-Stocks.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    return out_path


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: render_discord.py <scan_result.json> <output_dir>")
        sys.exit(1)

    out = save_payload(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Discord embed payload saved: {out}")