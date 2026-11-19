"""Spider's verify step: deterministic checks on the session's items before anything is posted. No model.

Each primary link must answer HTTP 200 after redirects (or be listed by its publisher's own feed this run) and
must never have been posted (the ledger); a story that moved cites its new page, so a posted link never repeats.
An item that fails is dropped. A discussion link that fails the same checks is removed and its item kept. The
session already holds the item count to spider_judge.MAX_ITEMS.

The links come from a session that reads untrusted pages, so a check refuses a host that resolves to anything
but a globally routable address, before the request and after any redirect (public_host says what that does not
cover), and a check that fails in any way drops its link rather than the run.
"""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from spider_floor import normalize_url

CHECK_TIMEOUT_S = 20
# Many publishers refuse an unknown client, so the check reads like a browser. It reads at most one block.
CHECK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Safari/605.1.15")


def public_host(url):
    """True when every address the URL's host resolves to is globally routable (not loopback, private, link-local,
    shared 100.64/10, reserved or multicast). urllib resolves the host again at connect, so a DNS answer that
    changes between the two lookups gets past this; what it can reach is a blind 1 KB GET whose only result is a
    yes or no."""
    try:
        host = urllib.parse.urlsplit(url).hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None)
        return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%", 1)[0]).is_global
                                   and not ipaddress.ip_address(i[4][0].split("%", 1)[0]).is_multicast
                                   for i in infos)
    except (OSError, UnicodeError, ValueError):
        return False


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(("https://", "http://")) or not public_host(newurl):
            raise urllib.error.URLError("redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PublicRedirects)


def url_resolves(url):
    """True when a GET to a public address answers 200 after redirects."""
    if not url.startswith(("https://", "http://")) or not public_host(url):
        return False
    req = urllib.request.Request(url, headers={"User-Agent": CHECK_UA, "Accept": "text/html,*/*"})
    try:
        with _OPENER.open(req, timeout=CHECK_TIMEOUT_S) as resp:
            resp.read(1024)
            return resp.status == 200
    except Exception:  # an odd URL or reply (InvalidURL, BadStatusLine, ...) drops its link, never the run
        return False


def verify(items, ledger, check=url_resolves, listed=frozenset()):
    """Returns (kept, dropped). dropped entries say what failed and why. A primary link in `listed` (one the
    publisher's own feed or API gave the floor this run) counts as resolving without a fetch: openai.com answers
    403 to any script, so a fetch would drop every OpenAI launch, though the page loads in a browser."""
    kept, dropped, seen = [], [], set()
    for it in items:
        try:
            url = normalize_url(it["url"])
        except ValueError:  # urlsplit refuses some shapes, e.g. "https://[x"
            dropped.append({"what": "item", "url": it["url"], "reason": "not a parseable link"})
            continue
        if url in ledger:
            dropped.append({"what": "item", "url": it["url"], "reason": "already posted"})
            continue
        if url in seen:
            dropped.append({"what": "item", "url": it["url"], "reason": "repeats a link in this post"})
            continue
        if url not in listed and not check(it["url"]):
            dropped.append({"what": "item", "url": it["url"], "reason": "does not resolve to HTTP 200"})
            continue
        it = dict(it)
        disc = it.get("discussion_url") or ""
        if disc:
            try:
                d = normalize_url(disc)
            except ValueError:
                d = None
            reason = ("not a parseable link" if d is None else "already posted" if d in ledger
                      else "repeats a link in this post" if d in seen or d == url
                      else None if check(disc) else "does not resolve to HTTP 200")
            if reason:
                dropped.append({"what": "discussion link", "url": disc, "reason": reason})
                it["discussion_url"] = ""
            else:
                seen.add(d)
        seen.add(url)
        kept.append(it)
    return kept, dropped
