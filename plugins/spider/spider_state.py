"""Spider's schedule and its state: the slots, the window, the posted-items ledger, the slot markers and
status.json.

Everything lives under one state folder (~/.local/state/spider by default):
  state.json     {"last_post_window_end": epoch}: where the next post's window starts
  ledger.json    {"items": [...]}: every posted item, kept 30 days
  markers/<slot> one empty file per slot that finished OK or SKIP, so a repeat run does nothing
  status.json    {"slots": {<slot>: {result, reason, epoch, at, run_dir}}}: one entry per slot (result OK, SKIP,
                 FAIL or MISSED), for a dead-man check to read
  results.log    one RESULT line per run
  runs/<run>/    each run's inputs, session output and would-be post

Slot ids are "<YYYY-MM-DD>-<slot name>" in config.json's time zone, the names coming from schedule.json ("am" and "pm" today). Stdlib only, for /usr/bin/python3 (3.9).
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(HERE, "schedule.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
CONFIG_KEYS = ("slack_channel", "keychain_service", "keychain_account", "time_zone", "reader")


def load_config(path=CONFIG_FILE):
    """The settings one install of Spider needs, from config.json: the Slack channel it posts to, the Keychain
    item (service and account) holding its bot token, the IANA time zone its slots and dates are in, and the
    reader's name the judge's prompt writes to. A missing, unknown or empty key, or a zone this Mac's zoneinfo
    doesn't know, raises ValueError, so a bad edit fails the install, never a run."""
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or set(raw) != set(CONFIG_KEYS):
        raise ValueError("%s: needs exactly the keys %s" % (path, ", ".join(CONFIG_KEYS)))
    for key in CONFIG_KEYS:
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError("%s: %s must be a non-empty string" % (path, key))
    try:
        ZoneInfo(raw["time_zone"])
    except Exception as e:  # ZoneInfoNotFoundError, or ValueError for a malformed key
        raise ValueError("%s: time_zone %r is not an IANA zone this Mac knows (%s)"
                         % (path, raw["time_zone"], type(e).__name__))
    return raw


CONFIG = load_config()
TZ = ZoneInfo(CONFIG["time_zone"])


def zone_offset_s(now, tz=TZ):
    """The configured zone's UTC offset at now, in seconds."""
    return int(datetime.fromtimestamp(now, tz).utcoffset().total_seconds())


def load_schedule(path=SCHEDULE_FILE):
    """(days, slots) from the schedule file, the one place Spider's run times are written: install.sh renders the
    LaunchAgent plist from it and this module reads it for SLOTS. days are launchd weekdays (1 Monday to 7 Sunday);
    slots are (name, hour, minute), returned in time order whatever order the file lists them (latest_slot takes the
    last one of a day). A malformed file raises ValueError, so a bad edit fails the install, never a run's slots."""
    with open(path) as f:
        raw = json.load(f)
    try:
        days = tuple(raw["days"])
        slots = tuple((s["name"], s["hour"], s["minute"]) for s in raw["slots"])
    except (KeyError, TypeError) as e:
        raise ValueError("%s: needs a days list and a slots list of {name, hour, minute} (%s: %s)"
                         % (path, type(e).__name__, e))
    if not days or not all(type(d) is int and 1 <= d <= 7 for d in days) or len(set(days)) != len(days):
        raise ValueError("%s: days must be distinct integers 1 to 7" % path)
    if not slots or len({name for name, _h, _m in slots}) != len(slots):
        raise ValueError("%s: slots must be non-empty with distinct names" % path)
    for name, hour, minute in slots:
        # A name becomes part of a marker file name and a run folder name, so it stays to lowercase letters, digits,
        # underscore and hyphen.
        if not (isinstance(name, str) and re.fullmatch(r"[a-z0-9_-]+", name) and type(hour) is int
                and type(minute) is int and 0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("%s: slot %r needs a name of a-z, 0-9, _ and -, an hour 0-23 and a minute 0-59"
                             % (path, name))
    if len({(hour, minute) for _name, hour, minute in slots}) != len(slots):
        raise ValueError("%s: two slots share a time" % path)
    return days, tuple(sorted(slots, key=lambda s: (s[1], s[2])))


def plist_intervals(days, slots):
    """The plist's StartCalendarInterval dicts, one line each, for install.sh: every day, every slot."""
    return "\n".join(
        "    <dict><key>Weekday</key><integer>%d</integer><key>Hour</key><integer>%d</integer>"
        "<key>Minute</key><integer>%d</integer></dict>" % (day, hour, minute)
        for day in days for _name, hour, minute in slots)


DAYS, SLOTS = load_schedule()
# The first run has no last post; it looks back one day.
FIRST_WINDOW_S = 24 * 3600
LEDGER_KEEP_S = 30 * 86400
STATUS_KEEP = 31 * len(SLOTS) + 2  # slot entries kept in status.json: a 31-day month of slots plus slack (64 at two a day)
# The dead-man rule (README "State"): a slot is missing when status.json has no OK or SKIP for
# it by its slot time plus this.
DEADMAN_S = 2 * 3600
# How far back to look for a slot: a week, with room to spare.
SLOT_SEARCH_S = 8 * 86400
EARLY_FIRE_S = 60


def slot_id(day, name):
    return "%s-%s" % (day.strftime("%Y-%m-%d"), name)


def slots_between(start, end):
    """Every (slot_id, name, epoch) with start <= epoch <= end, oldest first."""
    out = []
    day = datetime.fromtimestamp(start, TZ).date()
    last = datetime.fromtimestamp(end, TZ).date()
    while day <= last:
        if day.isoweekday() in DAYS:
            for name, hour, minute in SLOTS:
                t = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp()
                if start <= t <= end:
                    out.append((slot_id(day, name), name, t))
        day += timedelta(days=1)
    return out


def latest_slot(now):
    """The most recent slot at or before now. A run belongs to it, so a run that starts late (the Mac
    slept through 7:30 and woke after 1:30) is the afternoon slot, and one post covers both."""
    # A firing a moment before the slot's minute still belongs to that slot.
    slots = slots_between(now - SLOT_SEARCH_S, now + EARLY_FIRE_S)
    return slots[-1]


def _read(path, default):
    try:
        with open(path) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _write(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- the window

def last_post(state_dir):
    """Where the last successful post's window ended, or None before Spider's first post."""
    end = _read(os.path.join(state_dir, "state.json"), {}).get("last_post_window_end")
    return float(end) if isinstance(end, (int, float)) else None


def window_start(state_dir, now):
    """Everything since the last successful post; the first run looks back FIRST_WINDOW_S."""
    end = last_post(state_dir)
    return end if end is not None else now - FIRST_WINDOW_S


def record_success(state_dir, window_end):
    """After Slack returns ok: the next window starts where this one ended."""
    _write(os.path.join(state_dir, "state.json"), {"last_post_window_end": window_end})


# ---------------------------------------------------------------- the ledger

def load_ledger(state_dir):
    items = _read(os.path.join(state_dir, "ledger.json"), {}).get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def ledger_urls(state_dir):
    urls = set()
    for entry in load_ledger(state_dir):
        for key in ("url", "discussion_url"):
            if entry.get(key):
                urls.add(entry[key])
    return urls


def append_ledger(state_dir, items, now, post_ts):
    """Record a post's items. Called only after Slack returns ok. Drops entries older than 30 days."""
    # Imported here because spider_floor imports this module's UTC at load time.
    from spider_floor import normalize_url
    kept = [e for e in load_ledger(state_dir) if now - float(e.get("epoch") or 0) <= LEDGER_KEEP_S]
    date = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d")
    for it in items:
        kept.append({"url": normalize_url(it["url"]),
                     "discussion_url": normalize_url(it["discussion_url"]) if it.get("discussion_url") else "",
                     "event_key": it.get("event_key", ""), "topic": it.get("topic", ""),
                     "headline": it.get("headline", ""), "status": it.get("status", ""),
                     "date": date, "epoch": now, "post_ts": post_ts})
    _write(os.path.join(state_dir, "ledger.json"), {"items": kept})


# ---------------------------------------------------------------- markers and status

def has_marker(state_dir, slot):
    return os.path.exists(os.path.join(state_dir, "markers", slot))


def write_marker(state_dir, slot):
    os.makedirs(os.path.join(state_dir, "markers"), mode=0o700, exist_ok=True)
    with open(os.path.join(state_dir, "markers", slot), "w"):
        pass


def load_status(state_dir):
    status = _read(os.path.join(state_dir, "status.json"), {})
    if not isinstance(status.get("slots"), dict):
        status["slots"] = {}
    return status


def installed_at(state_dir):
    """The install time install.sh records once (and uninstall.sh clears), or 0 when there is no record."""
    try:
        with open(os.path.join(state_dir, "installed")) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def fold_missed(state_dir, slot, slot_epoch, since, now, run_dir):
    """After slot finishes OK or SKIP, record each earlier slot inside its window that never ran (the Mac slept
    through it, and launchd's one firing on wake became this slot) as MISSED, folded into this one. It walks back
    from this slot and stops at the first slot that has an entry, so a FAIL keeps its own reason. `since` is the
    last post's window end; before the first post there is none, and nothing is folded, since a slot before
    Spider's first post may predate its install."""
    if since is None:
        return
    # Never before this install either: after an uninstall the last post can predate a gap Spider wasn't running.
    floor = max(since, installed_at(state_dir), slot_epoch - SLOT_SEARCH_S)
    have = load_status(state_dir)["slots"]
    for sid, _name, _t in reversed(slots_between(floor, slot_epoch - 1)):
        if sid in have:
            break
        write_status(state_dir, sid, "MISSED", "folded into %s" % slot, now, run_dir)


def write_status(state_dir, slot, result, reason, now, run_dir):
    status = load_status(state_dir)
    status["slots"][slot] = {"result": result, "reason": reason, "epoch": now,
                             "at": datetime.fromtimestamp(now, TZ).isoformat(timespec="seconds"),
                             "run_dir": run_dir}
    status["slots"] = dict(sorted(status["slots"].items())[-STATUS_KEEP:])
    status["updated"] = now
    _write(os.path.join(state_dir, "status.json"), status)
