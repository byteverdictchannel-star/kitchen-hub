"""Fetch the RSS/Atom feeds listed in config.json and write news.json.

Also decides whether there is breaking news right now, and looks up the
current live video of the chosen YouTube news channel.

Runs in GitHub Actions. Standard library only.
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

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
        title = re.sub(r"\s+\|\s+CNN[\w ]*$", "", title)   # "... | CNN Politics"
        if not title:
            continue
        hay = f"{title} {link} {cats}".lower()
        if any(x in hay for x in exclude):
            continue
        dt = parse_date(date)
        out.append({"title": title, "source": feed["name"], "lang": feed.get("lang", "en"),
                    "time": dt.isoformat() if dt else None, "link": link})
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
    pattern = re.compile(cfg.get("breakingPattern",
        r"\bbreaking\b|^live\b|\blive updates\b|\bjust in\b"        # English
        r"|^urgent\b|\balerte\b|^en direct\b"                          # French
        r"|ultim['’ ]?ora|\bultima ora\b|^diretta\b"), re.I)         # Italian
    flag_window = timedelta(minutes=int(cfg.get("breakingFlagMinutes", 30)))
    recent = [i for i in all_items if i["time"] and NOW - datetime.fromisoformat(i["time"]) <= window]
    recent.sort(key=lambda x: x["time"], reverse=True)

    for it in recent:  # 1) the outlet itself calls it breaking/live, very recently
        if pattern.search(it["title"]) and NOW - datetime.fromisoformat(it["time"]) <= flag_window:
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


def send_alert(story, prev, cfg):
    """Phone alert through ntfy (free app). Once per story, even if the headline is reworded.

    The ntfy topic is a GitHub secret (NTFY_TOPIC), so it isn't visible in this public repo.
    Returns the updated list of alerted stories to keep in news.json.
    """
    alerted = [a for a in (prev.get("alerted") or [])
               if NOW - datetime.fromisoformat(a["at"]) < timedelta(hours=12)]
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    langs = cfg.get("alertLangs", ["en"])
    if not story or not topic or story.get("lang", "en") not in langs:
        return alerted
    w = words(story["title"])
    for a in alerted:
        old = set(a["words"])
        if a["id"] == story["id"] or len(w & old) >= 3 or (w and len(w & old) / len(w | old) >= 0.4):
            return alerted   # already told the phone about this story
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic, data=story["title"].encode("utf-8"), method="POST",
        headers={"Title": f"Breaking: {story['source']}".encode("utf-8"), "Priority": "high",
                 "Tags": "rotating_light", "Click": story.get("link") or cfg.get("phonePage", ""),
                 **({"Actions": f"view, Listen live, {cfg['phonePage']}"} if cfg.get("phonePage") else {})})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        print("ALERT sent:", story["title"])
        alerted.append({"id": story["id"], "words": sorted(w), "at": NOW.isoformat(timespec="seconds")})
    except Exception as e:
        print("WARN alert failed:", e, file=sys.stderr)
    return alerted[-30:]


def similar(a, b):
    """Two headlines about the same story (same language, shared key words)."""
    return len(a & b) >= 3 or (a and b and len(a & b) >= 2 and len(a & b) / len(a | b) >= 0.25)


def update_history(prev, per_feed, exclude):
    """Every headline from the last 16 hours, kept across runs so the night isn't lost
    when a feed only lists its latest few stories."""
    keep = NOW - timedelta(hours=16)
    seen, out = set(), []
    for it in [x for f in per_feed for x in f] + (prev.get("history") or []):
        if not it.get("time") or datetime.fromisoformat(it["time"]) < keep:
            continue
        if any(x in f"{it['title']} {it.get('link') or ''}".lower() for x in exclude):
            continue   # settings may have changed since it was saved
        it = dict(it, title=re.sub(r"\s+\|\s+CNN[\w ]*$", "", it["title"]))
        key = (it["source"], it["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append({k: it.get(k) for k in ("title", "source", "lang", "time", "link")})
    out.sort(key=lambda x: x["time"], reverse=True)
    return out[:400]


def update_breaking_log(prev, breaking):
    log = [b for b in (prev.get("breakingLog") or []) if NOW - datetime.fromisoformat(b["seen"]) < timedelta(hours=24)]
    if breaking and not any(b["id"] == breaking["id"] or similar(words(b["title"]), words(breaking["title"])) for b in log):
        log.append({**{k: breaking.get(k) for k in ("id", "title", "source", "lang", "time", "link", "reason")},
                    "seen": NOW.isoformat(timespec="seconds")})
    return log[-40:]


def overnight(history, blog, cfg, english_only=False):
    """Top stories since last night (nightStart, local time): the stories most outlets covered,
    with anything flagged as breaking first. For the morning "While you were asleep" card."""
    tz = ZoneInfo(cfg.get("timezone", "Asia/Dubai"))
    local = NOW.astimezone(tz)
    h, m = map(int, cfg.get("nightStart", "22:00").split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=1)
    items = [i for i in history if datetime.fromisoformat(i["time"]) >= start and (not english_only or i["lang"] == "en")]
    items.sort(key=lambda x: x["time"])
    clusters = []
    for it in items:
        w = words(it["title"])
        for c in clusters:
            if c["lang"] == it["lang"] and any(similar(w, mw) for mw in c["w"]):
                c["items"].append(it); c["w"].append(w); break
        else:
            clusters.append({"lang": it["lang"], "items": [it], "w": [w]})
    out = []
    for c in clusters:
        srcs = sorted({i["source"] for i in c["items"]})
        brk = any(any(similar(w, words(b["title"])) or b["title"] == i["title"] for b in blog
                      if datetime.fromisoformat(b["seen"]) >= start) for i, w in zip(c["items"], c["w"]))
        rep_ = max(c["items"], key=lambda i: i["time"])   # the latest wording of the story
        out.append({"title": rep_["title"], "source": rep_["source"], "lang": rep_["lang"], "link": rep_["link"],
                    "time": c["items"][0]["time"], "sources": srcs, "breaking": brk,
                    "score": (10 if brk else 0) + 3 * len(srcs) + len(c["items"])})
    out.sort(key=lambda x: (x["score"], x["time"]), reverse=True)
    # Keep the list varied: at most 2 stories per language unless there's nothing else.
    picked, per_lang = [], {}
    for o in out:
        if len(picked) >= 6:
            break
        if english_only or per_lang.get(o["lang"], 0) < (3 if o["lang"] == "en" else 2) or o["breaking"]:
            picked.append(o); per_lang[o["lang"]] = per_lang.get(o["lang"], 0) + 1
    for o in picked:
        o.pop("score")
    return {"since": start.isoformat(timespec="minutes"), "items": picked}

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def podcast_episodes(cfg, per_show=4):
    """Latest audio episodes from each podcast feed in config.json (for the phone app's Podcasts card)."""
    out = []
    for show in cfg.get("podcasts", []):
        try:
            ch = ET.fromstring(get(show["url"])).find("channel")
            for it in ch.findall("item")[:per_show]:
                enc = it.find("enclosure")
                if enc is None or not enc.get("url"):
                    continue
                d = (it.findtext(ITUNES + "duration") or "").strip()
                secs = 0
                for part in d.split(":"):
                    secs = secs * 60 + int(part) if part.isdigit() else secs
                t = parse_date(it.findtext("pubDate"))
                out.append({"show": show.get("short") or show["name"], "title": re.sub(r"\s*\|\s*Episode \d+\s*$", "", clean(it.findtext("title")))[:160],
                            "time": t.isoformat() if t else None, "audio": enc.get("url"), "dur": secs,
                            "link": (it.findtext("link") or "").strip()})
        except Exception as e:
            print("WARN podcast", show.get("name"), e, file=sys.stderr)
    out.sort(key=lambda x: x["time"] or "", reverse=True)
    return out[:6]


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

    try:
        prev = json.loads((ROOT / "news.json").read_text())
    except Exception:
        prev = {}
    alerted = send_alert(breaking, prev, cfg)
    history = update_history(prev, per_feed, exclude)
    blog = update_breaking_log(prev, breaking)

    # Phone page: newest English headlines, taking turns between sources.
    langs = cfg.get("alertLangs", ["en"])
    en = [[x for x in f if x["lang"] in langs] for f in per_feed]
    phone_items, seen2, i = [], set(), 0
    while len(phone_items) < 8 and any(i < len(f) for f in en):
        for f in en:
            if i < len(f) and len(phone_items) < 8 and f[i]["title"] not in seen2:
                seen2.add(f[i]["title"]); phone_items.append(f[i])
        i += 1
    phone_items.sort(key=lambda x: x["time"] or "", reverse=True)

    live = cfg.get("live") or None   # the page picks the channel; kept here so old pages still work

    data = {
        "updated": NOW.isoformat(timespec="seconds"),
        "items": picked,
        "breaking": breaking,
        "live": live,
        "errors": errors,
        "alerted": alerted,
        "phone": phone_items,
        "podcasts": podcast_episodes(cfg) or prev.get("podcasts", []),
        "overnight": overnight(history, blog, cfg),
        "overnightEn": overnight(history, blog, cfg, english_only=True),
        "breakingLog": blog,
        "history": history,
    }
    (ROOT / "news.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(picked)} headlines")


if __name__ == "__main__":
    main()
