#!/bin/bash
# Remove Spider's launchd agent and its installed scripts. Keeps the state folder (the ledger, status.json and
# the run folders) and the Keychain token.
set -euo pipefail

LABEL="com.tangled-shore.spider"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/.local/share/spider"
# A later install starts a new install time; the rest of the state stays.
rm -f "$HOME/.local/state/spider/installed"
echo "removed $LABEL (state kept in $HOME/.local/state/spider)"
