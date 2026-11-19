#!/usr/bin/env python3
"""Spider: the twice-daily AI news digest in Slack.

launchd starts `spider.py run` at each slot in schedule.json, in config.json's time zone (spider.plist.template). A run
belongs to the latest slot at or before its start, so a run launchd fires late on wake covers that slot,
and a slot that already finished makes a repeat run do nothing. Each run:

  1. waits up to 450 s for the network (no keep-awake tool of any kind);
  2. pulls the floor (spider_floor) for everything since the last successful post;
  3. runs one claude -p session to hunt, judge and write (spider_judge);
  4. verifies every link (spider_verify);
  5. posts as the Spider app (spider_post), retrying twice when Slack certainly did not post, and only after Slack
     returns ok writes the ledger, the next window's start, the slot's marker and status.json.

With no Spider token in the Keychain it does everything but the post, writes the would-be post to its run
folder and fails the slot (no-token). `--dry-run` does the same without touching any state: no ledger, window,
marker or status. Every run prints one line, `RESULT <slot> OK|SKIP|FAIL|DRYRUN <reason>`, and appends it to
results.log. Stdlib only, for /usr/bin/python3.

Usage: spider.py run [--dry-run] [--state-dir DIR]
"""
import argparse
import collections
import fcntl
import json
import os
import shutil
import signal
import sys
import time
import urllib.request
from datetime import datetime

import spider_floor
import spider_judge
import spider_post
import spider_state as state
import spider_verify

HERE = os.path.dirname(os.path.abspath(__file__))
# The launchd label; install.sh and uninstall.sh carry the same string.
LABEL = "com.tangled-shore.spider"
STATE_DIR = os.path.expanduser("~/.local/state/spider")
NETWORK_WAIT_S = 450
NETWORK_POLL_S = 15
# The whole run's wall clock, from each stage's own bound: the network wait, the floor's budget plus its slowest
# source, the session and its one repair, and verify (each link and its discussion link). The alarm is disarmed
# before the post, so it can never land between Slack's ok and the state writes.
VERIFY_BUDGET_S = 2 * spider_judge.MAX_ITEMS * spider_verify.CHECK_TIMEOUT_S
STAGE_BUDGETS_S = {"network": NETWORK_WAIT_S, "floor": spider_floor.FLOOR_BUDGET_S + spider_floor.SLOWEST_SOURCE_S,
                   "session and repair": 2 * spider_judge.TIMEOUT_S, "verify": VERIFY_BUDGET_S, "slack": 120}
RUN_TIMEOUT_S = sum(STAGE_BUDGETS_S.values())
# verify's reasons for dropping an item that are about repeats, not about a link that failed its check.
REPEAT_REASONS = ("already posted", "repeats a link in this post")
# A run that starts one slot and ends after the next began runs once more for the new slot, since launchd skips a
# firing while the job is still running.
MAX_RUNS_PER_START = 2

Deps = collections.namedtuple("Deps", "run_floor session check_url token post network_ready sleep utc_offset")
Deps.__new__.__defaults__ = (time.sleep, lambda now: time.localtime(now).tm_gmtoff)


class RunTimeout(BaseException):
    """The run's alarm. A BaseException, so no source's or check's `except Exception` can swallow it."""


def network_ready(wait_s=NETWORK_WAIT_S, sleep=time.sleep, urlopen=urllib.request.urlopen, clock=time.time):
    """True once slack.com answers; False after wait_s."""
    deadline = clock() + wait_s
    while True:
        try:
            with urlopen("https://slack.com/api/api.test", timeout=10) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        if clock() >= deadline:
            return False
        sleep(NETWORK_POLL_S)


def real_deps():
    claude = os.environ.get("SPIDER_CLAUDE") or shutil.which("claude") or "claude"
    return Deps(run_floor=spider_floor.run_floor,
                session=lambda prompt, cwd: spider_judge.run_session(claude, prompt, cwd),
                check_url=spider_verify.url_resolves, token=spider_post.keychain_token,
                post=spider_post.post_message, network_ready=network_ready)


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _run_file(run_dir, name, value):
    """One file in the run folder, a record for people; state files go through spider_state's atomic writer."""
    with open(os.path.join(run_dir, name), "w") as f:
        if isinstance(value, str):
            f.write(value)
        else:
            json.dump(value, f, indent=2, ensure_ascii=False)


def _local(epoch):
    return datetime.fromtimestamp(epoch, state.TZ).strftime("%a %b %d %Y, %I:%M %p")


def plain_text(message):
    """The post as plain text, for the run folder and reports."""
    return "\n\n".join(b["text"]["text"] for b in message["blocks"])


def run(state_dir, now, deps, dry_run=False, config_dir=HERE):
    """One run. Returns its RESULT line, which it also appends to results.log."""
    slot, slot_name, slot_epoch = state.latest_slot(now)
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    if not dry_run and state.has_marker(state_dir, slot):
        return _finish(state_dir, slot, "SKIP", "already-done", now, None, record=False)
    run_dir = os.path.join(state_dir, "runs", "%s-%s" % (slot, datetime.fromtimestamp(now, state.TZ)
                                                          .strftime("%H%M%S")) + ("-dry" if dry_run else ""))
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    record = not dry_run
    try:
        return _run(state_dir, now, deps, dry_run, config_dir, slot, slot_name, slot_epoch, run_dir, record)
    except RunTimeout:
        return _finish(state_dir, slot, "FAIL", "run timed out after %d s" % RUN_TIMEOUT_S, now, run_dir, record)
    except Exception as e:  # a crash is a failed slot with a reason, never a silent one
        return _finish(state_dir, slot, "FAIL", "crash (%s)" % type(e).__name__, now, run_dir, record)


def _run(state_dir, now, deps, dry_run, config_dir, slot, slot_name, slot_epoch, run_dir, record):
    summary = {"slot": slot, "started": now, "dry_run": dry_run}
    prior = state.last_post(state_dir)  # read before this run can record a post of its own

    def finish(result, reason, **extra):
        summary.update(extra, result=result, reason=reason, seconds=round(time.time() - now, 1))
        _run_file(run_dir, "run.json", summary)
        if record and result in ("OK", "SKIP"):
            state.fold_missed(state_dir, slot, slot_epoch, prior, now, run_dir)
        return _finish(state_dir, slot, result, reason, now, run_dir, record)

    # launchd fires at the slot times on the Mac's own clock, while slots are in config.json's time zone; when the
    # two offsets differ every run would land in the wrong slot, so a run refuses and says why.
    offset, want = deps.utc_offset(now), state.zone_offset_s(now)
    if offset != want:
        return finish("FAIL", "mac-clock-not-time-zone: the Mac is on UTC%+.1f h and config.json's %s is UTC%+.1f h, "
                      "so launchd's slot times are not the zone's" % (offset / 3600.0, state.CONFIG["time_zone"],
                                                                      want / 3600.0))
    if not deps.network_ready():
        return finish("FAIL", "network-not-ready")

    start = state.window_start(state_dir, now)
    summary["window"] = {"start": start, "end": now, "start_local": _local(start)}
    ledger_urls = state.ledger_urls(state_dir)
    sources = _read_json(os.path.join(config_dir, "sources.json"))
    creators = _read_json(os.path.join(config_dir, "creators.json"))
    candidates, statuses = deps.run_floor(sources, creators, start, ledger_urls)
    _run_file(run_dir, "candidates.json", candidates)
    _run_file(run_dir, "sources.json", statuses)
    summary["floor"] = {"candidates": len(candidates), "sources": {s["name"]: s["status"] for s in statuses}}
    if spider_floor.all_failed(statuses):
        return finish("FAIL", "all-sources-failed")

    with open(os.path.join(config_dir, "taste.md")) as f:
        taste = f.read()
    prompt = spider_judge.build_prompt(_local(start), _local(now), taste, candidates, state.load_ledger(state_dir),
                                       statuses)
    _run_file(run_dir, "prompt.txt", prompt)
    try:
        session = deps.session(prompt, run_dir)
    except spider_judge.JudgeError as e:
        return finish("FAIL", "judge: %s" % e)
    _run_file(run_dir, "session.json", session.get("raw") or session["output"])
    summary["session"] = {"cost_usd": session.get("cost_usd"), "seconds": session.get("seconds")}
    items = session["output"]["items"]
    found = len(candidates) + int(session["output"].get("hunt_added") or 0)
    if not items:
        return finish("SKIP", "no-new-items")

    kept, dropped = spider_verify.verify(items, ledger_urls, check=deps.check_url,
                                         listed=spider_floor.feed_urls(candidates))
    _run_file(run_dir, "verify.json", {"kept": kept, "dropped": dropped})
    summary["verify"] = {"from_session": len(items), "kept": len(kept), "dropped": len(dropped)}
    if not kept:
        # Every item a repeat of a posted or listed link is a quiet slot. Any link that failed its check is what a
        # broken network or check looks like: a FAIL keeps the slot unmarked and shows in status.json.
        broken = [d for d in dropped if d["what"] == "item" and d["reason"] not in REPEAT_REASONS]
        if not broken:
            return finish("SKIP", "no-new-items: every item repeated a posted link")
        return finish("FAIL", "no-verified-items: %d of %d links failed verification" % (len(broken), len(items)))

    message = spider_post.render(slot_name, now, kept, found)
    _run_file(run_dir, "post.json", message)
    _run_file(run_dir, "post.txt", plain_text(message))
    n = "%d item%s" % (len(kept), "" if len(kept) == 1 else "s")
    if dry_run:
        return finish("DRYRUN", "%s, would-be post at %s" % (n, os.path.join(run_dir, "post.txt")))
    token = deps.token()
    if not token:
        return finish("FAIL", "no-token: Keychain item %s is missing; would-be post at %s"
                      % (spider_post.KEYCHAIN_SERVICE, os.path.join(run_dir, "post.txt")))
    # From here the post and its record run to the end: the post is bounded by its own timeouts, and an alarm
    # landing after Slack's ok would leave a delivered post unrecorded.
    signal.alarm(0)
    try:
        ts = spider_post.publish(token, message, post=deps.post, sleep=deps.sleep)
    except spider_post.PostError as e:
        if not e.retryable:
            # The request may have posted; a retry could post twice within this run. Nothing is recorded
            # (the ledger is written only after Slack's ok), so the next slot covers these items again: if
            # Slack did store this post, they appear once more then. Spider chooses a possible repeat over a
            # possibly lost digest.
            return finish("FAIL", "post-unconfirmed: %s" % e)
        return finish("FAIL", "post-failed: %s" % e)
    # Slack said ok: only now does anything record the post. The ledger goes first, since it is what stops a repeat.
    try:
        state.append_ledger(state_dir, kept, now, ts)
        state.record_success(state_dir, now)
    except Exception as e:
        return finish("FAIL", "posted (ts %s) but recording it failed (%s); the next slot may repeat its items"
                      % (ts, type(e).__name__), post_ts=ts)
    failed = [s["name"] for s in statuses if s["status"] == "failed"]
    return finish("OK", n + ("; failed sources: %s" % ", ".join(failed) if failed else ""), post_ts=ts)


def _finish(state_dir, slot, result, reason, now, run_dir, record=True):
    reason = " ".join(str(reason).split())[:300]  # one line, whatever went into it
    line = "RESULT %s %s %s" % (slot, result, reason)
    if record:
        if result in ("OK", "SKIP"):
            state.write_marker(state_dir, slot)
        state.write_status(state_dir, slot, result, reason, now, run_dir)
    with open(os.path.join(state_dir, "results.log"), "a") as f:
        f.write("%s %s\n" % (datetime.fromtimestamp(now, state.TZ).isoformat(timespec="seconds"), line))
    return line


def _alarm(signum, frame):
    raise RunTimeout()


def main(argv=None, deps=None, clock=time.time):
    ap = argparse.ArgumentParser(prog="spider.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--dry-run", action="store_true", help="never post and write no state")
    p.add_argument("--state-dir", default=STATE_DIR)
    args = ap.parse_args(argv)
    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
    # A dry run writes no state, so it takes its own lock: it can never make a scheduled run skip its slot.
    lock = open(os.path.join(args.state_dir, "lock-dry" if args.dry_run else "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("spider: another run holds the lock; nothing done")
        return 0
    deps = deps or real_deps()
    signal.signal(signal.SIGALRM, _alarm)
    failed = False
    for _ in range(MAX_RUNS_PER_START):
        now = clock()
        signal.alarm(RUN_TIMEOUT_S)
        line = run(args.state_dir, now, deps, dry_run=args.dry_run)
        signal.alarm(0)
        print(line)
        failed = failed or " FAIL " in line
        if args.dry_run or state.latest_slot(clock())[0] == state.latest_slot(now)[0]:
            break
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
