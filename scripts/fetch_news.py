"""Fetch the RSS/Atom feeds listed in config.json and write news.json.

Also decides whether there is breaking news right now, and looks up the
current live video of the chosen YouTube news channel.

Runs in GitHub Actions. Standard library only.
"""
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATOM = "{http://www.w3.org/2005/Atom}"
UA = {"User-Agent": "Mozilla/5.0 (kitchen-dashboard)", "Accept-Language": "en"}
NOW = datetime.now(timezone.utc)

# Words that don't help tell two headlines apart.
STOP = set("""about after again against amid among also been being before between could
from have here into just more most news over says said some than that their there these
they this those through under until were what when where which while will with would
year years week first last live update updates says""".split())


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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


def fetch(feed, exclude):
    root = ET.fromstring(get(feed["url"]))
    raw = []
    for it in root.iter("item"):  # RSS 2.0
        raw.append((it.findtext("title"), it.findtext("link"),
                    it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"),
                    " ".join(c.text or "" for c in it.findall("category"))))
    for it in root.iter(ATOM + "entry"):  # Atom
        link = it.find(ATOM + "link")
        raw.append((it.findtext(ATOM + "title"), link.get("href") if link is not None else "",
                    it.findtext(ATOM + "updated") or it.findtext(ATOM + "published"), ""))
    out = []
    for title, link, date, cats in raw:
        title, link = clean(title), (link or "").strip()
        suffix = feed.get("stripSuffix")
        if suffix and title.endswith(suffix):  # Google News adds " - CNN"
            title = title[: -len(suffix)].strip()
        if not title:
            continue
        hay = f"{title} {link} {cats}".lower()
        if any(x in hay for x in exclude):
            continue
        dt = parse_date(date)
        out.append({"title": title, "source": feed["name"], "time": dt.isoformat() if dt else None, "link": link})
    out.sort(key=lambda x: x["time"] or "", reverse=True)
    return out


def words(title):
    return {w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in STOP}


def find_breaking(all_items, cfg):
    """Return the breaking story, or None.

    Breaking = a recent headline that says so ("Breaking", "Live:", "live updates"),
    or the same story carried by several different outlets within the window.
    """
    window = timedelta(minutes=int(cfg.get("breakingWindowMinutes", 60)))
    min_sources = int(cfg.get("breakingMinSources", 3))
    pattern = re.compile(cfg.get("breakingPattern", r"\bbreaking\b|^live\b|\blive updates\b|\bjust in\b"), re.I)
    recent = [i for i in all_items if i["time"] and NOW - datetime.fromisoformat(i["time"]) <= window]
    recent.sort(key=lambda x: x["time"], reverse=True)

    for it in recent:  # 1) the outlet itself calls it breaking/live
        if pattern.search(it["title"]):
            return dict(it, reason="flagged")

    best = None  # 2) several outlets on the same story
    for it in recent:
        w = words(it["title"])
        if len(w) < 2:
            continue
        sources = {it["source"]}
        for other in recent:
            if other["source"] in sources:
                continue
            if len(w & words(other["title"])) >= 2:
                sources.add(other["source"])
        if len(sources) >= min_sources and (best is None or len(sources) > best[1]):
            best = (it, len(sources))
    if best:
        return dict(best[0], reason=f"{best[1]} outlets")
    return None


def story_id(item):
    return re.sub(r"[^a-z0-9]+", "-", " ".join(sorted(words(item["title"])))[:80]) or "story"


def find_live_video(channel_id):
    """Current live video id of a YouTube channel (it changes every so often)."""
    try:
        html = get(f"https://www.youtube.com/channel/{channel_id}/live").decode("utf-8", "ignore")
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
        return m.group(1) if m else None
    except Exception as e:
        print("WARN live lookup", e, file=sys.stderr)
        return None


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    want = int(cfg.get("headlines", 5))
    exclude = [x.lower() for x in cfg.get("exclude", ["/sport"])]
    per_feed, errors = [], []
    for feed in cfg["feeds"]:
        try:
            per_feed.append(fetch(feed, exclude))
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

    breaking = find_breaking([x for f in per_feed for x in f], cfg)
    if breaking:
        breaking["id"] = story_id(breaking)
        print("BREAKING:", breaking["title"], f"({breaking['reason']})")

    live = None
    ch = cfg.get("live", {})
    if ch.get("channelId"):
        live = {"name": ch.get("name", ""), "channelId": ch["channelId"], "videoId": find_live_video(ch["channelId"])}

    data = {
        "updated": NOW.isoformat(timespec="seconds"),
        "items": picked,
        "breaking": breaking,
        "live": live,
        "errors": errors,
    }
    (ROOT / "news.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(picked)} headlines")


if __name__ == "__main__":
    main()
