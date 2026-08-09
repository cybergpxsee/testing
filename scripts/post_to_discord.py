#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

MAX_RETRIES = 3
RETRY_DELAY = 2


def post_discord(webhook: str, payload: dict) -> None:
    """Post Discord embed payload with retry on 429."""
    import time
    
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    
    for attempt in range(1, MAX_RETRIES + 1):
        req = Request(
            webhook,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "pullback-scan-github-action/1.0"
            },
            method='POST',
        )
        try:
            with urlopen(req, timeout=30) as resp:
                body = resp.read().decode('utf-8', errors='replace')
                if resp.status == 429:
                    # Rate limited - wait and retry
                    retry_after = 2
                    try:
                        retry_after = int(json.loads(body).get('retry_after', RETRY_DELAY))
                    except Exception:
                        pass
                    print(f"Rate limited (429), waiting {retry_after}s... (attempt {attempt}/{MAX_RETRIES})")
                    time.sleep(retry_after)
                    continue
                elif resp.status >= 400:
                    print(f"Discord webhook error: status={resp.status}, body={body}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY * attempt)
                        continue
                    raise SystemExit(f"Discord webhook failed after {MAX_RETRIES} attempts")
                print(f"Discord webhook status={resp.status}")
                if body:
                    print(body)
                return
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"Attempt {attempt} failed: {e}, retrying in {RETRY_DELAY * attempt}s...")
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise SystemExit(f"Discord webhook failed after {MAX_RETRIES} attempts: {e}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('Usage: post_to_discord.py <discord_embed_payload.json>')
    
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        raise SystemExit('DISCORD_WEBHOOK_URL is not set')
    
    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text(encoding='utf-8'))
    
    post_discord(webhook, payload)
    print("Discord embed posted successfully")


if __name__ == '__main__':
    main()
