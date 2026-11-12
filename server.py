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
from urllib.parse import urlparse, parse_qs

import feedparser

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDLIST = os.path.join(HERE, "feedlist.txt")
STATE_FILE = os.path.join(HERE, "feedcache.json")
CACHE_TTL = 4 * 3600     # seconds; ?fresh=1 or a browser hard reload bypasses
MAX_ENTRIES = 40         # per feed
WORKERS = 16
TIMEOUT = 8              # per feed, seconds
REDDIT_PER_BUILD = 2     # reddit.com throttles anonymous .rss hard
COOLDOWN = 180           # seconds before retrying a failed feed
REDDIT_COOLDOWN = 300
REDDIT_WARM_DELAY = 20   # seconds between startup Reddit fetches
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


def load_feeds():
    with open(FEEDLIST) as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]


def load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        if isinstance(data.get("sections"), dict):
            _state["sections"] = data["sections"]
        _state["cursor"] = int(data.get("cursor", 0))
    except Exception:
        pass


def save_state():
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(_state, fh)
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
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(4_000_000)


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
        entries = []
        raw_entries = parsed.get("entries") if isinstance(parsed, dict) else []
        for e in (raw_entries or [])[:MAX_ENTRIES]:
            t = str(e.get("title") or "").strip()
            l = str(e.get("link") or "").strip()
            if t and l:
                entries.append({"title": t, "link": l})
        if not entries:
            return url, None
        return url, {
            "id": _idpattern.sub("", title.replace(" ", "")),
            "title": title,
            "link": link,
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
    for url in feeds:
        section = fresh.get(url) or _state["sections"].get(url)
        if not section:
            failed.append(url)
            continue
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


def warm_reddit():
    """Reddit allows about one anonymous .rss fetch per window, so fill the
    missing subreddits slowly at startup instead of failing a burst."""
    feeds = [u for u in load_feeds() if is_reddit(u)]
    for url in feeds:
        if url in _state["sections"]:
            continue
        _, section = fetch_one(url)
        if section:
            _state["sections"][url] = section
            save_state()
        time.sleep(REDDIT_WARM_DELAY)
    with _lock:
        _cache["at"] = 0.0  # rebuild once so the page includes what we collected


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
        warm_reddit()

    threading.Thread(target=startup, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Feed Court live on http://localhost:%d/  (reload = fresh fetch)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")


if __name__ == "__main__":
    main()
