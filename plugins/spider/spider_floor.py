"""Spider's floor: the deterministic source pull. No model.

Reads sources.json (the labs' feeds, dev-tool GitHub releases, Hacker News over a points gate, Hugging Face
daily papers over an upvote gate) and creators.json (one YouTube feed per creator), keeps what was published
inside the window plus a lookback, normalizes every URL, merges candidates that share a URL, and drops any URL
already in the ledger. Each source gets a status: ok (items found), empty (fetched, nothing recent) or failed.

The lookback: a feed item can appear after its own date (a post backdated to its draft, a late feed cache), and a
gated item crosses its gate hours after it was created, so a window cut exactly at the last post would lose both.
The ledger already removes what was posted, so looking back further costs only prompt length.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from email.utils import parsedate_to_datetime

from spider_state import UTC

FETCH_TIMEOUT_S = 20
MAX_BYTES = 8 * 1024 * 1024
USER_AGENT = "Spider/1.0 (AI news digest)"
DROPPED_PARAMS = ("fbclid",)
EXCERPT_CHARS = 600
UTF16_32_BOMS = (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00", b"\xfe\xff", b"\xff\xfe")
VIDEOS_PAGE_COUNT = 15
YTDLP_TIMEOUT_S = 90
# How far before the window's start each kind looks (see the module docstring).
FEED_LOOKBACK_S = 6 * 3600
GATED_LOOKBACK_S = 48 * 3600
# The floor's own wall clock. A source that would start after it is recorded failed; the slowest single source
# (a YouTube feed timeout, then yt-dlp) adds FETCH_TIMEOUT_S + YTDLP_TIMEOUT_S past it.
FLOOR_BUDGET_S = 900
SLOWEST_SOURCE_S = FETCH_TIMEOUT_S + YTDLP_TIMEOUT_S


def normalize_url(url):
    """Lowercase scheme and host; strip utm_* and fbclid, the fragment and a trailing slash."""
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in DROPPED_PARAMS]
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                                    urllib.parse.urlencode(query), ""))


def http_get(url, timeout=FETCH_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_BYTES)
        # Some servers (deepmind.google) send gzip unasked, and urllib does not decode it. The output is capped,
        # so a hostile body can't inflate without bound.
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = inflate.decompress(body, 4 * MAX_BYTES)
            if inflate.unconsumed_tail:
                raise ValueError("gzip body inflates past %d bytes" % (4 * MAX_BYTES))
        return body


def _iso(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _rfc822(text):
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data):
    """RSS 2.0 items or Atom entries as [{title, url, published}]. An entry whose link won't parse is skipped."""
    # No feed needs entity declarations, and the stdlib parser's guard against entity expansion depends on the
    # system's expat, so a body that declares one is refused. The scan covers the whole body (a prolog may hold any
    # amount of comment before the DOCTYPE), and a UTF-16 or UTF-32 body is refused outright, since a byte scan
    # can't read it: by its BOM, or without one by the NUL bytes such a body opens with.
    if data.startswith(UTF16_32_BOMS) or b"\x00" in data[:4]:
        raise ValueError("feed is UTF-16 or UTF-32")
    if b"<!ENTITY" in data:
        raise ValueError("feed declares an XML entity")
    root = ET.fromstring(data)
    out = []
    for node in root.iter():
        kind = _local(node.tag)
        if kind not in ("item", "entry"):
            continue
        fields = {_local(c.tag): c for c in node}
        title = (fields["title"].text or "").strip() if "title" in fields else ""
        link = ""
        for c in node:
            if _local(c.tag) == "link":
                if c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
                if c.text and c.text.strip():
                    link = c.text.strip()
                    break
        published = None
        for key, parse in (("pubDate", _rfc822), ("published", _iso), ("updated", _iso), ("date", _iso)):
            if key in fields and fields[key].text:
                published = parse(fields[key].text)
                if published is not None:
                    break
        url = _safe_url(link)
        if url:
            out.append({"title": title, "url": url, "published": published})
    return out


def _safe_url(link):
    """The normalized link, or None when there is none or urlsplit refuses it (e.g. "https://[x")."""
    if not link:
        return None
    try:
        return normalize_url(link)
    except ValueError:
        return None


# ---------------------------------------------------------------- one function per kind: [candidate]

def _rss(src, since, fetch):
    return [e for e in parse_feed(fetch(src["url"])) if e["published"] is not None and e["published"] >= since]


def _github(src, since, fetch):
    data = json.loads(fetch("https://api.github.com/repos/%s/releases?per_page=10" % src["repo"]))
    out = []
    for r in data:
        published = _iso(r.get("published_at"))
        url = _safe_url(r.get("html_url"))
        if published is None or published < since or r.get("draft") or not url:
            continue
        out.append({"title": "%s %s" % (src["repo"], r.get("name") or r.get("tag_name")),
                    "url": url, "published": published,
                    "excerpt": (r.get("body") or "")[:EXCERPT_CHARS]})
    return out


def _hn(src, since, fetch):
    gate = int(src["min_points"])
    query = urllib.parse.urlencode({"tags": "story", "hitsPerPage": "100",
                                    "numericFilters": "created_at_i>%d,points>=%d" % (int(since), gate)})
    data = json.loads(fetch("https://hn.algolia.com/api/v1/search_by_date?" + query))
    out = []
    for h in data.get("hits", []):
        if int(h.get("points") or 0) < gate:  # the server applies the gate too; this holds if it ever doesn't
            continue
        discussion = "https://news.ycombinator.com/item?id=%s" % h["objectID"]
        url = _safe_url(h.get("url") or discussion)
        if not url:  # one bad submission stays out; the rest of the source stands
            continue
        out.append({"title": h.get("title") or "", "url": url,
                    "published": float(h.get("created_at_i") or 0), "points": h.get("points"),
                    "comments": h.get("num_comments"), "discussion_url": discussion})
    return out


def _hf(src, since, fetch):
    gate = int(src["min_upvotes"])
    data = json.loads(fetch("https://huggingface.co/api/daily_papers?limit=100"))
    out = []
    for entry in data:
        paper = entry.get("paper") or {}
        upvotes = int(paper.get("upvotes") or 0)
        published = _iso(paper.get("submittedOnDailyAt") or entry.get("publishedAt"))
        if upvotes < gate or published is None or published < since:
            continue
        out.append({"title": paper.get("title") or entry.get("title") or "",
                    "url": normalize_url("https://huggingface.co/papers/%s" % paper.get("id")),
                    "published": published, "upvotes": upvotes, "topic_hint": "research"})
    return out


def list_videos_page(channel_id):
    """The channel's public Videos page through yt-dlp: flat metadata only, nothing downloaded. Returns
    [{title, url, published}]. yt-dlp's approximate_date reads "2 days ago" as now minus 2 days, which is never
    earlier than the real upload, so filtering on it misses nothing in the window (it can let an older video
    through, which the session and the ledger drop)."""
    ytdlp = os.environ.get("SPIDER_YTDLP") or shutil.which("yt-dlp")
    if not ytdlp:
        raise OSError("yt-dlp not found")
    proc = subprocess.run([ytdlp, "--flat-playlist", "-J", "--playlist-end", str(VIDEOS_PAGE_COUNT),
                           "--extractor-args", "youtubetab:approximate_date",
                           "https://www.youtube.com/channel/%s/videos" % channel_id],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=YTDLP_TIMEOUT_S)
    if proc.returncode != 0:
        raise OSError("yt-dlp exit %d" % proc.returncode)
    out = []
    for e in json.loads(proc.stdout).get("entries") or []:
        if e.get("id"):
            out.append({"title": e.get("title") or "", "url": "https://www.youtube.com/watch?v=%s" % e["id"],
                        "published": float(e["timestamp"]) if e.get("timestamp") else None})
    return out


def _youtube(creator, since, fetch):
    """The channel's RSS feed first. YouTube's feed endpoint has intermittent outages that answer 404 for every
    channel, so any feed failure falls back to the Videos page instead of failing the source."""
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % creator["channel_id"]
    try:
        entries, via = parse_feed(fetch(url)), "rss"
    except Exception:
        entries, via = list_videos_page(creator["channel_id"]), "videos-page"
    return [dict(e, topic_hint="video", creator=creator.get("name", ""), via=via)
            for e in entries if e["published"] is not None and e["published"] >= since]


PULL = {"rss": _rss, "github_releases": _github, "hn": _hn, "hf_daily_papers": _hf, "youtube": _youtube}
LOOKBACK_S = {"hn": GATED_LOOKBACK_S, "hf_daily_papers": GATED_LOOKBACK_S}
# The publisher whose feed or API vouches for a link, by kind: an RSS feed vouches for links on its own host.
# Hacker News vouches for nothing, since its links are anyone's submissions.
PUBLISHER_HOST = {"github_releases": "github.com", "hf_daily_papers": "huggingface.co", "youtube": "youtube.com"}


def _bare_host(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def publisher_host(kind, src):
    """The host a source's own links live on, or None when the source vouches for no link."""
    if kind == "rss":
        return _bare_host(src["url"])
    return PUBLISHER_HOST.get(kind)


def vouched(url, kind, src):
    """True when the source is the link's own publisher: the link is on the publisher's host."""
    host = publisher_host(kind, src)
    return bool(host) and _bare_host(url) == host


def run_floor(sources, creators, start, ledger, fetch=http_get, clock=time.time):
    """Pull every source. Returns (candidates, statuses). A failed source never stops the others."""
    jobs = [(s.get("name", "?"), s.get("kind"), s) for s in sources.get("sources", [])]
    jobs += [("youtube:%s" % c.get("name", c["channel_id"]), "youtube", c) for c in creators.get("creators", [])]
    deadline = clock() + FLOOR_BUDGET_S
    merged, statuses = {}, []
    for name, kind, src in jobs:
        try:
            pull = PULL.get(kind)
            if pull is None:
                raise ValueError("unknown source kind %r" % kind)
            if clock() > deadline:
                raise TimeoutError("the floor's %d s budget ran out first" % FLOOR_BUDGET_S)
            found = pull(src, start - LOOKBACK_S.get(kind, FEED_LOOKBACK_S), fetch)
        except Exception as e:  # one source's failure is recorded, never raised
            statuses.append({"name": name, "status": "failed", "count": 0, "error": "%s: %s" % (type(e).__name__, e)})
            continue
        statuses.append({"name": name, "status": "ok" if found else "empty", "count": len(found)})
        for cand in found:
            if cand["url"] in ledger:
                continue
            # A link its own publisher's feed or API lists, on that publisher's host, is known to exist.
            from_feed = vouched(cand["url"], kind, src)
            have = merged.get(cand["url"])
            if have is None:
                cand["sources"] = [name]
                cand["from_feed"] = from_feed
                merged[cand["url"]] = cand
                continue
            have["sources"].append(name)
            have["from_feed"] = have["from_feed"] or from_feed
            for key, value in cand.items():
                if key != "sources" and not have.get(key):
                    have[key] = value
    for cand in merged.values():
        if cand.get("published") is not None:
            cand["published_at"] = datetime.fromtimestamp(cand["published"], UTC).isoformat(timespec="minutes")
    return list(merged.values()), statuses


def feed_urls(candidates):
    """The candidates' links that their own publisher's feed or API listed, on the publisher's host."""
    return {c["url"] for c in candidates if c.get("from_feed")}


def all_failed(statuses):
    return bool(statuses) and all(s["status"] == "failed" for s in statuses)
