#!/usr/bin/env python3
"""Live feed server for Feed Court.

Fetches every URL in feedlist.txt on demand and hands the result to index.html,
so reloading the page pulls fresh data instead of reading a stale static file.

Reddit throttles unauthenticated .rss to roughly one request per window, so one
Reddit feed is refreshed per rebuild and the rest keep their last good copy,
which is persisted in feedcache.json.

Usage:
    ./server.py [port]        (default port 8111)
    open http://localhost:8111/
"""
import calendar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import feedparser

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDLIST = os.path.join(HERE, "feedlist.txt")
STATE_FILE = os.path.join(HERE, "feedcache.json")
CACHE_TTL = 4 * 3600     # seconds; ?fresh=1 or a browser hard reload bypasses
STATE_VERSION = 7        # bump when the cached entry shape changes
MAX_ENTRIES = 40         # per feed
WORKERS = 16
TIMEOUT = 8              # per feed, seconds
REDDIT_PER_BUILD = 2     # reddit.com throttles anonymous .rss hard
COOLDOWN = 180           # seconds before retrying a failed feed
REDDIT_COOLDOWN = 300
REDDIT_WARM_DELAY = 20   # seconds between startup Reddit fetches
WARM_REDDIT_PER_CYCLE = 3  # subreddits retried per warm cycle, keeps 429s away
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

_cache = {"payload": None, "at": 0.0}
_lock = threading.Lock()
_hosts_lock = threading.Lock()
_host_locks = {}
_state = {"sections": {}, "cursor": 0, "cooldowns": {}}
_idpattern = re.compile(r"[\W_]+")


def is_reddit(url):
    return "reddit.com" in urlparse(url).netloc.lower()


def feed_override(url):
    """A few feeds need a short label: Reuters and AP arrive through Google News."""
    plain = unquote(url)
    if "news.google.com/rss/search" in plain:
        if "site:reuters.com" in plain:
            return {
                "title": "Reuters",
                "link": "https://www.reuters.com/",
                "domain": "reuters.com",
                "strips": (" - Reuters",),
            }
        if "site:apnews.com" in plain:
            return {
                "title": "AP News",
                "link": "https://apnews.com/",
                "domain": "apnews.com",
                "strips": (" - AP News", " - AP"),
            }
    return None


def image_is_junk(url):
    low = url.lower()
    if low.endswith(".gif") or ".gif?" in low:
        return True
    return any(junk in low for junk in (
        "/emoji/", "twitter_icon", "gravatar", "avatar", "/logo", "logo.", "icon.",
        "banner_library", "placeholder", "spacer", "1x1", "tracking", "doubleclick",
        "analytics", "feedburner",
        # pixel only as a tracking-pixel filename, never as part of a host name
        # such as petapixel.com
        "pixel.gif", "pixel.png", "pixel.jpg", "pixel.jpeg", "pixel.webp",
        "/pixel?", "_pixel.", "-pixel.", "tracking_pixel", "clear.gif",
    ))


def entry_image(entry):
    """Best available image for an entry: prefer declared size, then big URL hints."""
    candidates = []
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key, []) or []:
            url = media.get("url")
            kind = str(media.get("type", ""))
            if not url:
                continue
            if not (kind.startswith("image") or not kind or
                    url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))):
                continue
            if image_is_junk(url):
                continue
            try:
                width = int(media.get("width") or 0)
            except (TypeError, ValueError):
                width = 0
            if 0 < width < 200:
                continue
            candidates.append((width, url))
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            if not image_is_junk(link.get("href") or ""):
                candidates.append((0, link.get("href")))
    if not candidates:
        blob = ""
        for block in entry.get("content", []) or []:
            blob += block.get("value", "") or ""
        blob += entry.get("summary", "") or ""
        for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)', blob):
            if not image_is_junk(match.group(1)):
                candidates.append((0, match.group(1)))
                break
    if not candidates:
        return None

    def score(item):
        width, url = item
        value = width / 1000.0
        low = url.lower()
        if any(hint in low for hint in ("1200", "1024", "1600", "1500", "2048", "original", "large")):
            value += 3
        if any(hint in low for hint in ("/240/", "thumbnail", "-150x150", "-300x", "/small")):
            value -= 2
        return value

    candidates.sort(key=score, reverse=True)
    return candidates[0][1]


def entry_is_video(entry):
    link = entry.get("link") or ""
    if "youtube.com/watch" in link or "youtu.be/" in link or "/shorts/" in link:
        return True
    video_exts = (".mp4", ".webm", ".mov", ".m3u8")
    for block in entry.get("media_content", []) or []:
        url = (block.get("url") or "").lower().split("?")[0]
        if str(block.get("type", "")).startswith("video") and url.endswith(video_exts):
            return True
    for link2 in entry.get("links", []) or []:
        url = (link2.get("href") or "").lower().split("?")[0]
        if link2.get("rel") == "enclosure" and str(link2.get("type", "")).startswith("video") \
                and url.endswith(video_exts):
            return True
    return False


def reddit_label(url):
    """Short heading for a subreddit: r/linux, not the feed's long title."""
    match = re.search(r"/r/([^/]+)", url, re.I)
    return "r/" + match.group(1) if match else "reddit"


# Domain suffixes where the registrable part is three labels, not two.
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "co.nz", "co.za",
    "com.au", "com.br", "com.cn", "co.in", "com.mx", "co.il",
}


def site_domain(url):
    """Short domain for a heading: no scheme, no www, no subdomains."""
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def source_key(url):
    """One feed per source, except where the path or query names the source."""
    host = urlparse(url).netloc.lower()
    if "reddit.com" in host:
        return host + urlparse(url).path.lower()
    if "youtube.com" in host:
        return url.lower()
    if "news.google.com" in host:
        return url.lower()
    if "feedburner" in host or "feedproxy" in host:
        return url.lower()
    return site_domain(url)


def load_feeds():
    with open(FEEDLIST) as fh:
        raw = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    seen, feeds = set(), []
    for url in raw:
        key = source_key(url)
        if key in seen:
            continue
        seen.add(key)
        feeds.append(url)
    return feeds


def load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        if data.get("version") != STATE_VERSION:
            return  # entries from before images/video, refetch instead
        if isinstance(data.get("sections"), dict):
            _state["sections"] = data["sections"]
        _state["cursor"] = int(data.get("cursor", 0))
    except Exception:
        pass


def save_state():
    tmp = STATE_FILE + ".tmp"
    payload = dict(_state)
    payload["version"] = STATE_VERSION
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        sys.stderr.write("state save failed: %s\n" % exc)


def host_lock(url):
    host = urlparse(url).netloc.lower()
    with _hosts_lock:
        lock = _host_locks.get(host)
        if lock is None:
            lock = _host_locks[host] = threading.Lock()
        return host, lock


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    # YouTube serves intermittent 404/500 for its Atom feeds; one retry usually
    # clears it, and a missing channel would shrink the video row.
    attempts = 3 if "youtube.com/feeds" in url else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read(4_000_000)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5 + 10 * attempt)


def fetch_one(url):
    host, lock = host_lock(url)
    try:
        with lock:
            if is_reddit(url):
                time.sleep(2.5)
            try:
                raw = fetch_bytes(url)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                time.sleep(5.0)
                raw = fetch_bytes(url)
        parsed = feedparser.parse(raw)
        feed = parsed.get("feed") if isinstance(parsed, dict) else None
        feed = feed if isinstance(feed, dict) else {}
        title = str(feed.get("title") or "").strip() or urlparse(url).netloc
        link = str(feed.get("link") or url).strip()
        override = feed_override(url)
        if override:
            title, link = override["title"], override["link"]
        entries = []
        raw_entries = parsed.get("entries") if isinstance(parsed, dict) else []
        for e in (raw_entries or [])[:MAX_ENTRIES]:
            t = str(e.get("title") or "").strip()
            l = str(e.get("link") or "").strip()
            if override and override.get("strips"):
                for suffix in override["strips"]:
                    t = t.replace(suffix, "").strip()
            if t and l:
                stamp = e.get("published_parsed") or e.get("updated_parsed")
                try:
                    published = calendar.timegm(stamp) if stamp else None
                except Exception:
                    published = None
                entries.append({
                    "title": t,
                    "link": l,
                    "image": entry_image(e),
                    "video": entry_is_video(e),
                    "published": published,
                })
        if not entries:
            return url, None
        return url, {
            "id": _idpattern.sub("", title.replace(" ", "")),
            "title": title,
            "link": link,
            "domain": override["domain"] if override else site_domain(link or url),
            "entries": entries,
        }
    except Exception as exc:
        sys.stderr.write("feed failed %s: %s: %s\n" % (url, type(exc).__name__, exc))
        return url, None


def build():
    feeds = load_feeds()
    started = time.time()
    now = time.time()
    cooldowns = _state.setdefault("cooldowns", {})
    for gone in [u for u in _state["sections"] if u not in feeds]:
        _state["sections"].pop(gone, None)
    due = [u for u in feeds if now >= cooldowns.get(u, 0)]
    reddit_due = [u for u in due if is_reddit(u)]
    tasks = [u for u in due if not is_reddit(u)]
    if reddit_due:
        count = min(REDDIT_PER_BUILD, len(reddit_due))
        start = _state["cursor"] % len(reddit_due)
        tasks += [reddit_due[(start + i) % len(reddit_due)] for i in range(count)]
        _state["cursor"] = (start + count) % len(reddit_due)

    fresh = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for url, section in pool.map(fetch_one, tasks):
            if section:
                fresh[url] = section
                _state["sections"][url] = section
                cooldowns.pop(url, None)
            else:
                cooldowns[url] = now + (REDDIT_COOLDOWN if is_reddit(url) else COOLDOWN)

    sections, failed, stale = [], [], 0
    seen_links = set()
    for url in feeds:
        section = fresh.get(url) or _state["sections"].get(url)
        if not section:
            failed.append(url)
            continue
        link_key = (section.get("link") or "").rstrip("/").lower()
        if link_key and link_key in seen_links:
            continue  # second feed URL pointing at the same site, e.g. a feedburner alias
        seen_links.add(link_key)
        if is_reddit(url):
            label = reddit_label(url)
            section = dict(section, title=label, domain="",
                           id=_idpattern.sub("", label))
        elif not section.get("domain"):
            section = dict(section, domain=site_domain(section.get("link") or url))
        if url not in fresh:
            stale += 1
            section = dict(section, stale=True)
        sections.append(section)

    save_state()
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "elapsed": round(time.time() - started, 1),
        "feeds_total": len(feeds),
        "feeds_ok": len(sections),
        "stale": stale,
        "failed": failed,
        "sections": sections,
    }


def get_payload(force=False):
    with _lock:
        age = time.time() - _cache["at"]
        fresh_cache = age < CACHE_TTL
        if _cache["payload"] and fresh_cache and not force:
            out = dict(_cache["payload"])
            out["cached"] = True
            out["age"] = int(age)
            out["ttl"] = CACHE_TTL
            return out
        payload = build()
        _cache.update(payload=payload, at=time.time())
        out = dict(payload)
        out["cached"] = False
        out["age"] = 0
        out["ttl"] = CACHE_TTL
        return out


_warm_reddit_cursor = 0


def warm_missing():
    """Retry feeds that have no cached section. Reddit is retried a few at a
    time because anonymous .rss rate limits by IP, and one burst of sixteen
    subreddits just earns a wall of 429s. Returns how many were recovered."""
    global _warm_reddit_cursor
    feeds = load_feeds()
    added = 0
    missing_reddit = [u for u in feeds if is_reddit(u) and u not in _state["sections"]]
    if missing_reddit:
        count = min(WARM_REDDIT_PER_CYCLE, len(missing_reddit))
        start = _warm_reddit_cursor % len(missing_reddit)
        for i in range(count):
            url = missing_reddit[(start + i) % len(missing_reddit)]
            _, section = fetch_one(url)
            if section:
                _state["sections"][url] = section
                save_state()
                added += 1
            time.sleep(REDDIT_WARM_DELAY)
        _warm_reddit_cursor = (start + count) % len(missing_reddit)
    for url in feeds:
        if url in _state["sections"]:
            continue
        _, section = fetch_one(url)
        if section:
            _state["sections"][url] = section
            save_state()
            added += 1
        time.sleep(3)
    if added:
        with _lock:
            _cache["at"] = 0.0  # rebuild once so the page includes what we collected
    return added


class Handler(BaseHTTPRequestHandler):
    server_version = "FeedCourt/2.0"

    def log_message(self, format, *args):  # noqa: A002 (http.server signature)
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), format % args))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/feeds":
            force = parse_qs(parsed.query).get("fresh", ["0"])[0] == "1"
            try:
                body = json.dumps(get_payload(force=force)).encode()
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/":
            path = "/index.html"
        safe = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
        if not safe.startswith(HERE) or not os.path.isfile(safe):
            self._send(404, b"not found", "text/plain")
            return
        ctypes = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".gif": "image/gif",
            ".txt": "text/plain; charset=utf-8",
        }
        ext = os.path.splitext(safe)[1].lower()
        with open(safe, "rb") as fh:
            body = fh.read()
        if safe.endswith("index.html"):
            # Browsers send Cache-Control: no-cache (plus Pragma) on a hard reload,
            # and max-age=0 on a normal one. The page reads this flag to decide
            # whether to bypass the server side cache.
            cc = (self.headers.get("Cache-Control", "") + " " +
                  self.headers.get("Pragma", "")).lower()
            body = body.replace(b"__FRESH_BOOT__", b"true" if "no-cache" in cc else b"false")
        self._send(200, body, ctypes.get(ext, "application/octet-stream"))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8111
    load_state()

    def startup():
        get_payload(force=True)
        # keep retrying feeds that failed: YouTube 404s and Reddit 429s clear on
        # their own, and a missing channel leaves a hole in a video row
        while True:
            warm_missing()
            time.sleep(600)

    threading.Thread(target=startup, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Feed Court live on http://localhost:%d/  (reload = fresh fetch)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")


if __name__ == "__main__":
    main()
