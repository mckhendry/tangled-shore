"""Spider's post: render the Block Kit message and send it as the Spider app.

The bot token is read only from the macOS Keychain (the item and account config.json names) on the child's
stdout. It lives in memory and goes out only in the Authorization
header; it is never printed, logged or written.

publish() is the one way a digest leaves Spider: today it posts to Slack.
"""
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from spider_judge import MAX_CHARS, UNSAFE_URL
from spider_state import CONFIG, SLOTS, TZ

# A slot's name is free text in schedule.json, so the post's "Morning" or "Afternoon" follows its hour.
SLOT_HOURS = {name: hour for name, hour, _minute in SLOTS}

CHANNEL = CONFIG["slack_channel"]
KEYCHAIN_SERVICE = CONFIG["keychain_service"]
KEYCHAIN_ACCOUNT = CONFIG["keychain_account"]
POST_WAITS_S = (10, 30)  # two retries
SECURITY_BIN = os.environ.get("SPIDER_SECURITY_BIN", "/usr/bin/security")
TOKEN_PREFIX = "xoxb-"
HTTP_TIMEOUT_S = 20
EMOJI = {"models": ":brain:", "devtools": ":hammer_and_wrench:", "research": ":page_facing_up:",
         "business": ":moneybag:", "video": ":movie_camera:"}
THIN = 6  # fewer kept items than this is a thin day


class PostError(Exception):
    """A post that failed. retryable is True only when Slack certainly did not post: it answered ok:false or a 4xx, or
    the connection never opened. A timeout or reset after the request went out may have posted, so a retry could
    post the digest twice."""

    def __init__(self, reason, retryable=True):
        super().__init__(reason)
        self.retryable = retryable


def keychain_token():
    """The Spider bot token, or None when the Keychain has no usable one."""
    try:
        proc = subprocess.run([SECURITY_BIN, "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s",
                               KEYCHAIN_SERVICE, "-w"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not token.startswith(TOKEN_PREFIX) or len(token) == len(TOKEN_PREFIX):
        return None
    return token


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url):
    """A URL safe inside Slack's <url|label>: <, >, |, whitespace and control characters percent-encoded.
    spider_judge.check_output already refuses such links; this is the second guard at the point of use."""
    return UNSAFE_URL.sub(lambda m: "%%%02X" % ord(m.group()), url)


def _cap(text, field):
    """The second guard on spider_judge.MAX_CHARS: a section Slack would refuse fails the whole post."""
    cap = MAX_CHARS[field]
    return text if len(text) <= cap else text[:cap - 1] + "…"


def _host(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or "link"
    except ValueError:
        host = "link"
    return host[4:] if host.startswith("www.") else host


def render(slot_name, now, items, found):
    """The message: {"text", "blocks"}. A header with found and kept counts, then one section per item."""
    day = datetime.fromtimestamp(now, TZ)
    part = "Morning" if SLOT_HOURS.get(slot_name, 12) < 12 else "Afternoon"
    title = ":spider: %s intel, %s %s %d" % (part, day.strftime("%a"), day.strftime("%b"), day.day)
    kept = len(items)
    counts = "%d kept of %d found" % (kept, found)
    if kept < THIN:
        counts += " · Quiet %s: %d item%s" % (part.lower(), kept, "" if kept == 1 else "s")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*%s*\n%s" % (title, counts)}}]
    for it in items:
        links = "<%s|%s>" % (link(it["url"]), esc(_host(link(it["url"]))))
        if it.get("discussion_url"):
            links += " · <%s|discussion>" % link(it["discussion_url"])
        text = "%s *%s*\n%s _%s_\n%s" % (EMOJI.get(it["topic"], ":small_blue_diamond:"),
                                          esc(_cap(it["headline"], "headline")), esc(_cap(it["why"], "why")),
                                          it["status"], links)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return {"text": "%s: %s" % (title, counts), "blocks": blocks}


def post_message(token, message):
    """chat.postMessage as the Spider app. Returns the message ts; raises PostError unless Slack says ok."""
    body = dict(message, channel=CHANNEL, unfurl_links=False, unfurl_media=False)
    req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Authorization": "Bearer " + token,
                                                         "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # A 4xx (429 included) is a refusal before any work, so it never posted. A 5xx (a 502 or 504 from a proxy)
        # can come after Slack stored the message, so it is unconfirmed, like a timeout.
        raise PostError("http_%d" % e.code, retryable=e.code < 500)
    except urllib.error.URLError as e:
        # urlopen wraps every OSError from connecting and sending. Only a refused connection or a failed DNS lookup
        # is certain to come before Slack read anything; a broken pipe or an abort may come mid-send.
        before_send = isinstance(e.reason, (ConnectionRefusedError, socket.gaierror))
        raise PostError("transport: %s" % type(e.reason).__name__, retryable=before_send)
    except (OSError, ValueError) as e:
        raise PostError("unconfirmed: %s" % type(e).__name__, retryable=False)
    if not data.get("ok"):
        raise PostError(str(data.get("error") or "unknown_error"))
    return data.get("ts") or ""


def publish(token, message, post=post_message, sleep=time.sleep):
    """Send one digest: post it, retrying twice when Slack certainly did not post. Returns the post's ts. Raises
    the last PostError when every try failed, or at once when one may have posted (retryable False), so one run
    never posts twice."""
    for attempt in range(len(POST_WAITS_S) + 1):
        try:
            return post(token, message)
        except PostError as e:
            if not e.retryable or attempt == len(POST_WAITS_S):
                raise
            sleep(POST_WAITS_S[attempt])
