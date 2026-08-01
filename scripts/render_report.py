#!/usr/bin/env python3
import json
import sys
from pathlib import Path


def render(out: dict) -> str:
    lines = []
    generated_at = str(out.get('generated_at_utc', ''))
    report_date = generated_at[5:10] if len(generated_at) >= 10 else '未知'
    data_sources = out.get('data_sources') or [
        'Nasdaq Trader 月更股票池快取',
        'Yahoo Finance / yfinance 日線 OHLCV',
    ]
    data_sources = [str(x).replace('日线', '日線').replace('数据', '數據').replace('代码', '代碼') for x in data_sources]

    long_top_20m_to_50m = out.get('top10_long_20m_to_50m', []) or []
    short_top_20m_to_50m = out.get('top10_short_20m_to_50m', []) or []
    long_top_50m_plus = out.get('top10_long_50m_plus', []) or []
    short_top_50m_plus = out.get('top10_short_50m_plus', []) or []
    all_rows = long_top_20m_to_50m + short_top_20m_to_50m + long_top_50m_plus + short_top_50m_plus

    def fmt_mmdd(date_str: str) -> str:
        s = str(date_str)
        return s[5:10] if len(s) >= 10 else s

    def display_dates(row: dict) -> str:
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

    def build_text_table(rows: list[dict], direction: str) -> str:
        body = []
        if rows:
            for idx, row in enumerate(rows[:10], start=1):
                body.append({
                    '序': f'{idx:02d}',
                    '代碼': str(row.get('symbol', '')),
                    '方向': direction,
                    '回調日': display_dates(row),
                })
        else:
            body.append({'序': '--', '代碼': '無', '方向': direction, '回調日': '-'})

        headers = ['序', '代碼', '方向', '回調日']
        widths = {h: max(len(h), max(len(str(r[h])) for r in body)) for h in headers}

        def fmt_row(r: dict) -> str:
            return '  '.join(str(r[h]).ljust(widths[h]) for h in headers)

        sep = '  '.join('─' * widths[h] for h in headers)
        table_lines = [fmt_row({h: h for h in headers}), sep]
        table_lines.extend(fmt_row(r) for r in body)
        return '```text\n' + '\n'.join(table_lines) + '\n```'

    def add_single_group(title: str, icon: str, rows: list[dict], direction: str) -> None:
        lines.append(f'{icon} {title}')
        lines.append('')
        label = '🟢 做多前10' if direction == '做多' else '🔴 做空前10'
        lines.append(label)
        lines.append(build_text_table(rows, direction))
        lines.append('')

    lines.append('📊 美股回調交易形態簡報')
    lines.append('')
    lines.append(f"🗂️ 數據來源：{'；'.join(data_sources)}")
    lines.append(f'📅 數據日期：{report_date}')
    lines.append('')

    if not all_rows:
        lines.append('⚪ 今日無符合「確認後回調再介入」條件，且回調日過去20日平均交易額達2000萬美元以上的標的。')
        lines.append('')
        lines.append('⚠️ 風險提示：這是AI掃描出的參考買賣點，不涉及投資建議，做多或做空都有風險')
        return '\n'.join(lines)

    lines.append('')
    lines.append('🟢 回調買榜單')
    lines.append('')
    add_single_group('過去20日平均交易額：5000萬美元以上', '💎', long_top_50m_plus, '做多')
    add_single_group('過去20日平均交易額：2000萬-5000萬美元', '⚙️', long_top_20m_to_50m, '做多')
    lines.append('🔴 賣出／做空榜單')
    lines.append('')
    add_single_group('過去20日平均交易額：5000萬美元以上', '💎', short_top_50m_plus, '做空')
    add_single_group('過去20日平均交易額：2000萬-5000萬美元', '⚙️', short_top_20m_to_50m, '做空')
    lines.append('⚠️ 風險提示：這是AI掃描出的參考買賣點，不涉及投資建議，做多或做空都有風險')
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
