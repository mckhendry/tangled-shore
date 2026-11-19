#!/bin/bash
# Install (or reinstall) Spider's launchd agent on this Mac: `spider.py run` at each slot in schedule.json.
# Who runs it and when: README "Setup" (after the Spider app exists and its bot token is in the
# Keychain item config.json names).
#
# Renders the plist's run times from schedule.json, which spider_state.py also reads, so change a time there.
# Copies the scripts, config.json, sources.json, creators.json, schedule.json and taste.md to a stable path outside the
# plugin cache, which a plugin update replaces, so re-run it after editing the creator list or updating the
# plugin. Refuses to install when the Keychain holds no Spider token; it checks the token's shape and never
# prints it. It does not run Spider: the first post comes at the next slot.
set -euo pipefail

LABEL="com.tangled-shore.spider"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="config.json sources.json creators.json taste.md schedule.json $(cd "$HERE" && echo spider*.py)"
BIN_DIR="$HOME/.local/share/spider"
# The same path spider.py reads.
STATE_DIR="$HOME/.local/state/spider"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

for f in $FILES; do
  [ -f "$HERE/$f" ] || { echo "install: missing $HERE/$f" >&2; exit 1; }
done
/usr/bin/python3 -c 'import sys' || { echo "install: /usr/bin/python3 does not run (Command Line Tools missing?)" >&2; exit 1; }
CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || { echo "install: no claude on PATH" >&2; exit 1; }
# Optional: a creator whose RSS feed fails falls back to its Videos page through yt-dlp.
YTDLP="$(command -v yt-dlp || true)"
[ -n "$YTDLP" ] || echo "install: no yt-dlp on PATH; a creator whose feed fails is a failed source" >&2

# The plist's StartCalendarInterval comes from schedule.json, the file spider_state.py reads for its slots. Every
# Spider module imports spider_state, so a malformed file would also fail the token check below; this runs first
# to say so, before anything is copied.
SCHEDULE="$(/usr/bin/python3 -B -c \
  'import sys; sys.path.insert(0, sys.argv[1]); import spider_state as s; print(s.plist_intervals(s.DAYS, s.SLOTS))' \
  "$HERE")" || { echo "install: $HERE/schedule.json is unreadable or malformed, or config.json is" >&2; exit 1; }
TEMPLATE="$(cat "$HERE/spider.plist.template")"

# The same read and shape rule a run uses (spider_post.keychain_token); the token stays inside that process.
if ! /usr/bin/python3 -B -c \
    'import sys; sys.path.insert(0, sys.argv[1]); import spider_post; sys.exit(0 if spider_post.keychain_token() else 1)' \
    "$HERE"; then
  SERVICE="$(/usr/bin/python3 -B -c \
    'import sys; sys.path.insert(0, sys.argv[1]); import spider_post; print(spider_post.KEYCHAIN_SERVICE)' "$HERE")"
  echo "install: Keychain item $SERVICE holds no Spider bot token (xoxb-); store it first (README \"Setup\")" >&2
  exit 1
fi

mkdir -p -m 700 "$BIN_DIR" "$STATE_DIR"
# The first install's time, kept across re-installs: a dead-man check never calls a slot before it "has no result".
[ -s "$STATE_DIR/installed" ] || date +%s > "$STATE_DIR/installed"
for f in $FILES; do
  install -m 600 "$HERE/$f" "$BIN_DIR/$f"
done
mkdir -p "$HOME/Library/LaunchAgents"

printf '%s\n' "${TEMPLATE/@SCHEDULE@/$SCHEDULE}" | \
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@BIN@|$BIN_DIR|g" -e "s|@STATE@|$STATE_DIR|g" \
  -e "s|@CLAUDE@|$CLAUDE|g" -e "s|@CLAUDE_DIR@|$(dirname "$CLAUDE")|g" -e "s|@YTDLP@|$YTDLP|g" \
  > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout can return before teardown finishes, and an immediate bootstrap then fails with an I/O error.
for attempt in 1 2 3 4 5; do
  if launchctl bootstrap "$DOMAIN" "$PLIST"; then
    break
  fi
  if [ "$attempt" = 5 ]; then
    echo "install: launchctl bootstrap failed for $LABEL" >&2
    exit 1
  fi
  sleep 1
done
echo "installed $LABEL"
echo "  plist:   $PLIST"
echo "  scripts: $BIN_DIR"
echo "  state:   $STATE_DIR (status.json, results.log, runs/)"
echo "  slots:   the days and times in schedule.json; the first post comes at the next slot"
