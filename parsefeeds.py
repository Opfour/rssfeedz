"""Static snapshots: static.html (grid) and jumble.html (single column).

Both pages embed the same payload the live server serves and run the same
renderer, feedcourt-app.js, so the snapshot layout matches the live wall
instead of drifting away from it. Only the data source differs: the live page
calls /api/feeds, the snapshots carry the JSON inline.
"""

import datetime
import json
import os
import socket
import sys
import time
from random import shuffle

import server

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
APP = "feedcourt-app.js"

# feedparser has no timeout of its own; a dead host can otherwise stall cron
socket.setdefaulttimeout(10)

PAUSE = 0.5          # between feeds, keeps Reddit and YouTube from throttling
REDDIT_PAUSE = 6.0   # anonymous .rss rate limits by IP


def collect():
    """Reuse the live server's cached sections and fetch only what is missing.

    A cold run hammers 98 hosts in a row and Reddit answers with a wall of 429s,
    which is how the snapshot ended up missing whole sections. feedcache.json
    already holds the last good copy of every feed, so read that first."""
    urls = server.load_feeds()
    server.load_state()
    cached = server._state.get("sections", {})
    sections, failed, seen_links = [], [], set()
    started = time.time()
    fetched = reused = 0
    for url in urls:
        section = cached.get(url)
        if section:
            reused += 1
        else:
            try:
                _, section = server.fetch_one(url)
            except Exception as exc:                  # noqa: BLE001 - one feed must not stop the run
                print("  fail %s: %s" % (url, exc))
                section = None
            if section:
                fetched += 1
            time.sleep(REDDIT_PAUSE if server.is_reddit(url) else PAUSE)
        if not section:
            failed.append(url)
            continue
        link_key = (section.get("link") or "").rstrip("/").lower()
        if link_key and link_key in seen_links:
            continue                                  # same site behind two feed URLs
        seen_links.add(link_key)
        sections.append(server.normalize_section(url, section))
    print("reused %d cached sections, fetched %d" % (reused, fetched))
    return {
        "updated": datetime.datetime.now(datetime.UTC).isoformat(),
        "elapsed": round(time.time() - started, 1),
        "feeds_total": len(urls),
        "feeds_ok": len(sections),
        "stale": 0,
        "failed": failed,
        "sections": sections,
    }


def page(payload, view):
    """index.html with the payload inlined: same markup, same renderer."""
    html = open(INDEX, encoding="utf-8").read()
    if '<meta http-equiv="refresh"' not in html:
        html = html.replace(
            '<title>Feed Court</title>',
            '<title>Feed Court</title>\n  <meta http-equiv="refresh" content="600">',
        )
    # the live page asks the server whether this was a hard reload; a file with
    # no server behind it has nothing to ask
    html = html.replace(
        '<script>window.__freshBoot = (function(){ try { return __FRESH_BOOT__; } '
        'catch (e) { return false; } })();</script>', ""
    )
    blob = json.dumps(payload).replace("</", "<\\/")
    inject = '<script id="payload" type="application/json">%s</script>\n' % blob
    if view == "wire":
        inject += '<script>window.__VIEW__ = "wire";</script>\n'
    html = html.replace('<script src="%s"></script>' % APP, inject + '<script src="%s"></script>' % APP)
    return html


def write(name, html):
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("wrote %s (%d bytes)" % (name, len(html)))


def main():
    payload = collect()
    print("feeds ok %d/%d, failed %d, %.1fs" % (
        payload["feeds_ok"], payload["feeds_total"], len(payload["failed"]), payload["elapsed"]))
    write("static.html", page(payload, "grid"))
    # jumble keeps the shuffled single column the old snapshot had
    shuffled = dict(payload, sections=payload["sections"][:])
    shuffle(shuffled["sections"])
    write("jumble.html", page(shuffled, "wire"))
    return 0 if payload["sections"] else 1


if __name__ == "__main__":
    sys.exit(main())
