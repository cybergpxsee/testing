#!/usr/bin/env python3
"""
動量排名報告渲染器
從 momentum_rank_output.json 生成 Markdown 和 Discord Embed
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd


def render_markdown(json_path: Path, output_path: Path) -> str:
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    scan_info = data.get('scan_info', {})
    categories = data.get('categories', {})
    
    cat1 = pd.DataFrame(categories.get('category1_20R60R_75_89_120R_lt80', []))
    cat2 = pd.DataFrame(categories.get('category2_20R60R_ge90_120R_lt80', []))
    cat3 = pd.DataFrame(categories.get('category3_rank_ge90', []))
    
    # 確保按 Rank 排序
    for cat in [cat1, cat2, cat3]:
        if len(cat) > 0 and 'Rank' in cat.columns:
            cat.sort_values('Rank', ascending=False, inplace=True)
    
    lines = []
    lines.append("📊 **美股動量排名週報**")
    lines.append("")
    lines.append(f"📅 掃描日期：{scan_info.get('scan_date', '未知')}")
    lines.append(f"🔢 掃描標的數：{scan_info.get('universe_total', 0)}")
    lines.append(f"✅ 有效數據：{scan_info.get('valid_count', 0)}")
    lines.append(f"📈 數據來源：Nasdaq Trader + Yahoo Finance (yfinance)")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    def render_category(cat_df, title, icon):
        lines.append(f"## {icon} {title} （共 {len(cat_df)} 檔）")
        lines.append("")
        if len(cat_df) > 0:
            lines.append("| 代碼 | 20R | 60R | 120R | Rank |")
            lines.append("|------|-----|-----|------|------|")
            for _, row in cat_df.iterrows():
                lines.append(f"| {row['Symbol']} | {int(row['20R'])} | {int(row['60R'])} | {int(row['120R'])} | {row['Rank']:.1f} |")
        else:
            lines.append("*無符合條件標的*")
        lines.append("")
    
    render_category(cat1, "類別 1：20R&60R在 75-89，但 120R < 80", "🟡")
    render_category(cat2, "類別 2：20R&60R ≥ 90，但 120R < 80", "🟢")
    render_category(cat3, "類別 3：總 Rank ≥ 90", "🔵")
    
    lines.append("---")
    lines.append("")
    lines.append("⚠️ **風險提示**：此為動量排名篩選結果，非買賣建議。排名基於相對 SPY 的超額報酬百分位，數值越大代表相對動量越強。請自行判斷風險。")
    
    result = "\n".join(lines)
    output_path.write_text(result, encoding='utf-8')
    return result


def render_discord(json_path: Path, output_path: Path) -> dict:
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    scan_info = data.get('scan_info', {})
    categories = data.get('categories', {})
    
    cat1 = pd.DataFrame(categories.get('category1_20R60R_75_89_120R_lt80', []))
    cat2 = pd.DataFrame(categories.get('category2_20R60R_ge90_120R_lt80', []))
    cat3 = pd.DataFrame(categories.get('category3_rank_ge90', []))
    
    for cat in [cat1, cat2, cat3]:
        if len(cat) > 0 and 'Rank' in cat.columns:
            cat.sort_values('Rank', ascending=False, inplace=True)
    
    def format_category(df, title):
        if len(df) == 0:
            return {"name": title, "value": "無符合條件標的", "inline": False}
        
        display_df = df.head(20)
        lines = []
        for _, row in display_df.iterrows():
            lines.append(f"`{row['Symbol']}` 20R:{int(row['20R'])} 60R:{int(row['60R'])} 120R:{int(row['120R'])} Rank:{row['Rank']:.1f}")
        
        value = "\n".join(lines)
        if len(df) > 20:
            value += f"\n... 共 {len(df)} 檔，僅顯示前 20"
        
        return {"name": title, "value": value, "inline": False}
    
    embed = {
        "title": "📊 美股動量排名週報",
        "description": f"掃描日期：{scan_info.get('scan_date', '未知')} | 標的：{scan_info.get('valid_count', 0)}/{scan_info.get('universe_total', 0)}",
        "color": 0x3498db,
        "fields": [
            format_category(cat1, "🟡 類別1：20R&60R 75-89, 120R<80"),
            format_category(cat2, "🟢 類別2：20R&60R ≥90, 120R<80"),
            format_category(cat3, "🔵 類別3：Rank ≥ 90"),
        ],
        "footer": {"text": "相對 SPY 超額報酬百分位排名 | 非投資建議"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    payload = {"embeds": [embed]}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: render_momentum_report.py input.json output_dir')
    
    json_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 生成 Markdown
    md_path = output_dir / 'momentum_rank_report.md'
    render_markdown(json_path, md_path)
    
    # 生成 Discord Embed
    discord_path = output_dir / 'momentum_discord_embed.json'
    render_discord(json_path, discord_path)
    
    print(f"Generated: {md_path} and {discord_path}")