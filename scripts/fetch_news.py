"""Fetch the RSS/Atom feeds listed in config.json and write news.json.

Runs in GitHub Actions. Standard library only.
"""
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATOM = "{http://www.w3.org/2005/Atom}"


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)  # RSS style
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom style
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean(text):
    text = unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch(feed):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "kitchen-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):  # RSS 2.0
        items.append((it.findtext("title"), it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")))
    for it in root.iter(ATOM + "entry"):  # Atom
        items.append((it.findtext(ATOM + "title"), it.findtext(ATOM + "updated") or it.findtext(ATOM + "published")))
    out = []
    for title, date in items:
        title = clean(title)
        if title:
            dt = parse_date(date)
            out.append({"title": title, "source": feed["name"], "time": dt.isoformat() if dt else None})
    out.sort(key=lambda x: x["time"] or "", reverse=True)
    return out


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    want = int(cfg.get("headlines", 5))
    per_feed, errors = [], []
    for feed in cfg["feeds"]:
        try:
            per_feed.append(fetch(feed))
        except Exception as e:  # one broken feed shouldn't kill the rest
            errors.append(f"{feed['name']}: {e}")
            print("WARN", feed["name"], e, file=sys.stderr)

    # Take turns between sources so every source gets a slot, newest first.
    picked, seen, i = [], set(), 0
    while len(picked) < want and any(i < len(f) for f in per_feed):
        for f in per_feed:
            if i < len(f) and len(picked) < want and f[i]["title"] not in seen:
                seen.add(f[i]["title"])
                picked.append(f[i])
        i += 1
    picked.sort(key=lambda x: x["time"] or "", reverse=True)

    if not picked:
        print("No headlines fetched; keeping previous news.json", file=sys.stderr)
        sys.exit(0)
    data = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": picked, "errors": errors}
    (ROOT / "news.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(picked)} headlines")


if __name__ == "__main__":
    main()
