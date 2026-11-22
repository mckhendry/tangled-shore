# Spider

Spider posts one AI technology and news digest to a Slack channel at each slot in `schedule.json` (7:30 AM and
1:30 PM, every day, as shipped), as its own Slack app. Each post covers everything since the last successful post:
new models, dev-tool releases, breakout papers, big business moves and new videos from the creators you follow.

It runs on macOS under launchd, posts only to Slack, and needs the `claude` CLI. What it's for, and what it won't
grow into, is in [`VISION.md`](VISION.md).

## How a run works

`spider.py run` (launchd starts it at each slot):

1. **Slot.** The run belongs to the latest slot at or before its start. A slot that already finished OK or SKIP
   has a marker, and a repeat run does nothing. launchd fires a slot missed while the Mac slept once on wake, so a
   morning run that lands after the afternoon slot is the afternoon slot, and the two make one post.
2. **Clock.** Slots are in `config.json`'s `time_zone`, while launchd fires on the Mac's own clock. When the two
   offsets differ, the run fails the slot as `mac-clock-not-time-zone` and says both offsets.
3. **Network.** It waits up to 450 s for the network. Spider uses no keep-awake tool of any kind.
4. **Floor** (`spider_floor.py`, no model). It pulls `sources.json` (OpenAI and Google DeepMind feeds, Claude
   Code, Codex and Gemini CLI releases, Hacker News at 150 points or more, Hugging Face daily papers at 25
   upvotes or more) and one YouTube feed per creator in `creators.json`, inside the window plus a lookback (6 h for
   feeds, 48 h for Hacker News and Hugging Face, so an item that appears late or crosses its gate late isn't lost).
   It normalizes URLs, merges candidates that share a URL, drops any URL already posted, and records each source
   as ok, empty or failed. Only every source failing aborts the slot.
5. **Session, the judge** (`spider_judge.py`). One `claude -p` session on Sonnet 5.5 at high effort, in the run's
   folder, with WebSearch and WebFetch only (its inputs are in the prompt, so it has no file tool), a 1200 s
   timeout that kills its whole process group, a `--max-budget-usd 5` flag (a cap on usage-billed logins;
   unverified on a subscription, where the timeout is the hard stop), and the blank `~/.claude-clean` profile. It
   hunts for what the floor can't see, groups identical events, drops what was already posted unless the story
   moved, and writes up to 8 items as JSON held to a schema. An answer that fails the check gets one repair prompt
   on the same session, then the slot fails.
6. **Verify** (`spider_verify.py`, no model). Each primary link must answer HTTP 200 after redirects, or be listed
   this run by its own publisher's feed on that publisher's host, and must not have been posted before; a failing
   item is dropped. A failing discussion link is removed and its item kept.
7. **Post** (`spider_post.py`'s `publish`). It posts as the Spider app, retrying twice when Slack certainly did
   not post (it answered `ok: false` or a 4xx, or the connection never opened). A timeout or a 5xx after the
   request went out fails the slot as `post-unconfirmed` with no retry, so one run never posts twice. Only after
   Slack returns `ok` does it write the ledger, the next window's start and the slot's marker.

Each run prints and appends to `results.log` one line: `RESULT <slot> OK|SKIP|FAIL|DRYRUN <reason>`. Spider never
posts an error into the news channel.

## The rubric

The judge scores candidates against `taste.md`, in its priority order. The shipped `taste.md` is one reader's
example; rewrite it for yourself. As shipped:

1. Models and labs: new models, major releases, pricing and availability, policy that changes what you can use.
2. Dev tools: Claude Code first, then other coding agents, IDEs, SDKs and MCP; release notes only when they change
   how you work.
3. Research papers: only breakouts that the labs, Hacker News or the Hugging Face list are talking about.
4. Funding and business: only big moves, such as raises over $1B, acquisitions, or exec departures at the major
   labs.
5. New videos from the creators in `creators.json`, one line each.

Two rules hold whatever the taste says: popularity is evidence, not importance, and announced is not shipped, so
each item says which it is. A post has 6 to 8 items, fewer on a thin day, and none when nothing new matters.

## Setup

You need macOS, `/usr/bin/python3` (the Command Line Tools), the `claude` CLI on your PATH, a Slack workspace
where you can add an app, and optionally `yt-dlp` (a creator whose RSS feed fails falls back to its Videos page
through it).

1. **Get the files.** `git clone https://github.com/mckhendry/tangled-shore` and work in
   `tangled-shore/plugins/spider`. (If you installed the plugin with `/plugin install spider@tangled-shore`, the same
   files are in Claude Code's plugin cache, but a plugin update replaces that folder, so keep your edits in a clone.)
2. **Create the Slack app.** Create an app named Spider with the bot scope `chat:write`, install it to your
   workspace, and invite it to the channel it should post in. Put that channel's id in `config.json` as
   `slack_channel`.
3. **Store its bot token in the Keychain.** The Bot User OAuth Token starts `xoxb-`. With the default
   `keychain_service` and `keychain_account` from `config.json`:

   ```
   security add-generic-password -s spider-slack-token -a spider -w
   ```

   With `-w` last and no value, `security` prompts for the token, so it never lands in your shell history.
4. **Log in the judge's profile once.** The judge runs `claude` with `CLAUDE_CONFIG_DIR=~/.claude-clean`, a blank
   profile with no instructions, memory or plugins of yours. It needs its own login before the first run:

   ```
   CLAUDE_CONFIG_DIR=~/.claude-clean claude
   ```

   then run `/login` and quit. Without it, the judge's session can't start work, and each slot fails with a
   `judge:` reason.
5. **Edit the settings** (all strings) in `config.json`. Every value it ships with is an example to replace:
   - `slack_channel`: the channel id from step 2.
   - `keychain_service`, `keychain_account`: the Keychain item from step 3.
   - `time_zone`: your IANA zone, such as `America/New_York`. It ships as `UTC`, so a Mac whose clock isn't at
     UTC's offset fails every slot until you set it.

   The loader refuses a missing, extra or empty key, or a zone your Mac doesn't know. Then edit `taste.md`,
   `creators.json` (one `{"name", "channel_id"}` per YouTube channel) and, if you like, `schedule.json`.
6. **Install.** Run `./install.sh`. It refuses when the Keychain holds no Spider token, copies the files to
   `~/.local/share/spider`, and loads the launchd job `com.tangled-shore.spider`. It does not run Spider; the first
   post comes at the next slot. Re-run it after any edit. `./uninstall.sh` removes the job and the copied files
   and keeps the state, except its install-time record.

To see a post before any goes out, run a dry run (below).

## Run times

`schedule.json` holds them: `days` (launchd weekdays, 1 Monday to 7 Sunday) and `slots` (name, hour, minute).
`install.sh` renders the plist's `StartCalendarInterval` from it, and `spider_state.py` reads it, so a time changes
in that one file; re-run `install.sh` afterward. The loader rejects a bad file (an unknown day, a duplicate name or
time, a name outside `a-z0-9_-`, an hour past 23). Keep slots farther apart than one run's wall clock
(`spider.RUN_TIMEOUT_S`) plus two hours. A slot's name is part of its id, so renaming one starts new slot ids.

## State

Everything lives in `~/.local/state/spider/`:

- `status.json`: one entry per slot (`result`, `reason`, `epoch`, `run_dir`). A slot with no `OK` or `SKIP` entry
  by its slot time plus 2 hours (`spider_state.DEADMAN_S`) is missing; point a check of your own at this file to
  hear about it, since Spider never posts its own errors.
- `ledger.json`: every posted item (URL, discussion URL, event key, topic, headline, date, post ts), kept 30 days.
- `state.json`: where the next window starts.
- `markers/<slot>`, `results.log`, `launchd.log`, and `runs/<slot>-<time>/`, each run's inputs, session output,
  verify result and would-be post (`post.json`, `post.txt`).

## Dry run

`/usr/bin/python3 -B spider.py run --dry-run --state-dir <dir>` does everything but the post: it writes the
would-be post to its run folder and writes no ledger, window, marker or status. A scheduled run with no token in
the Keychain does the same, except it records the slot as `FAIL no-token`.

## Tests

From `plugins/spider`: `python3 -m unittest discover -s tests -p 'test_*.py' -t tests`. They're offline: every
fetch, the session, Slack and the Keychain are fakes.
