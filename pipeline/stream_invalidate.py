"""Listen to Wikimedia's live EventStreams RecentChanges and invalidate cached MariaDB rows
whenever a tracked Wikipedia article is edited.

Run as a standalone background worker:
  .venv/bin/python -m pipeline.stream_invalidate
"""
import json
import re

import requests
from dotenv import load_dotenv

load_dotenv()
from api.consistency import invalidate_entity  # noqa: E402

STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"


def run():
    print(f"Listening to {STREAM_URL} ...", flush=True)
    with requests.get(
        STREAM_URL,
        headers={"User-Agent": "Aikakone/0.1 (cache invalidator)"},
        stream=True,
        timeout=(10, 90),
    ) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            try:
                ev = json.loads(raw[6:])
            except Exception:
                continue
            if ev.get("namespace") != 0 or ev.get("type") not in ("edit", "new"):
                continue
            m = re.fullmatch(r"([a-z]{2})wiki", ev.get("wiki", ""))
            if not m:
                continue
            lang, title = m.group(1), ev.get("title", "")
            if invalidate_entity(lang=lang, title=title):
                print(f"Invalidated {lang}:{title}", flush=True)


if __name__ == "__main__":
    run()
