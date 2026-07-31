#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

MAX_CONTENT = 1900


def split_message(text: str, max_len: int = MAX_CONTENT) -> list[str]:
    blocks = [b.strip() for b in text.strip().split('\n\n') if b.strip()]
    if not blocks:
        return []
    parts = []
    current = ''
    for block in blocks:
        candidate = block if not current else current + '\n\n' + block
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ''
        if len(block) <= max_len:
            current = block
            continue
        # fallback: hard split very large blocks
        lines = block.splitlines()
        chunk = ''
        for line in lines:
            candidate = line if not chunk else chunk + '\n' + line
            if len(candidate) <= max_len:
                chunk = candidate
            else:
                if chunk:
                    parts.append(chunk)
                chunk = line
        if chunk:
            current = chunk
    if current:
        parts.append(current)
    return parts


def post_text(webhook: str, text: str) -> None:
    payload = json.dumps({"content": text}, ensure_ascii=False).encode('utf-8')
    req = Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pullback-scan-github-action/1.0"},
        method='POST',
    )
    with urlopen(req, timeout=30) as resp:
        body = resp.read().decode('utf-8', errors='replace')
        print(f'Discord webhook status={resp.status}')
        if body:
            print(body)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('Usage: post_to_discord.py path/to/report.md')

    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        raise SystemExit('DISCORD_WEBHOOK_URL is not set')

    report_path = Path(sys.argv[1])
    text = report_path.read_text(encoding='utf-8').strip()
    if not text:
        raise SystemExit('report markdown is empty')

    parts = split_message(text)
    if not parts:
        raise SystemExit('report markdown is empty after split')
    for idx, part in enumerate(parts, start=1):
        post_text(webhook, part)
        print(f'Discord webhook part={idx}/{len(parts)}')


if __name__ == '__main__':
    main()
