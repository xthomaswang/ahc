#!/bin/sh
# AHC live agent feed (Claude Code): passes the hook input to agent_feed.py, but only in an AHC task
# (an .ahc/ folder, or an AHC tool being called), so every other session pays nothing but this test.
[ "${AHC_FEED:-1}" = "0" ] && exit 0
input=$(cat)
case "$input" in
  *mcp__plugin_ahc_ahc__*) ;;
  *) [ -d "${CLAUDE_PROJECT_DIR:-$PWD}/.ahc" ] || exit 0 ;;
esac
printf '%s' "$input" | /usr/bin/env python3 "$(dirname "$0")/agent_feed.py" "$@" >/dev/null 2>&1
exit 0
