"""Post messages to a Discord webhook."""
from __future__ import annotations

import time

import requests

from .util import log

SUPPRESS_EMBEDS = 1 << 2


def post_to_discord(webhook_url: str, messages: list[str], session: requests.Session | None = None,
                    pause: float = 0.6) -> None:
    s = session or requests.Session()
    for i, content in enumerate(messages, 1):
        payload = {
            "content": content,
            "username": "Opening Brief",
            "flags": SUPPRESS_EMBEDS,          # no link preview cards
            "allowed_mentions": {"parse": []},  # never ping anyone
        }
        for attempt in (1, 2):
            r = s.post(webhook_url, params={"wait": "true"}, json=payload, timeout=20)
            if r.status_code == 429 and attempt == 1:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                log.warning("Discord rate limited; waiting %.1fs", wait)
                time.sleep(min(wait, 30))
                continue
            if r.status_code >= 500 and attempt == 1:
                time.sleep(2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Discord webhook returned {r.status_code}: {r.text[:200]}")
            break
        log.info("Sent Discord message %d/%d (%d chars)", i, len(messages), len(content))
        time.sleep(pause)
