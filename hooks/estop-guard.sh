#!/bin/sh
# AHC emergency stop, Claude Code side: before every tool call, if this task folder's stop is
# engaged, end the agent's turn (all tools, not only AHC's). The MCP server refuses AHC actions on
# its own; this stops the rest of the agent too. A no-op, in a few milliseconds, everywhere else.
# The task folder is the server's: AHC_WORKSPACE (as the server reads it), else the project folder.
folder="${AHC_WORKSPACE:-${CLAUDE_PROJECT_DIR:-$PWD}}"
case "$folder" in
  "~") folder="$HOME" ;;
  "~/"*) folder="$HOME/${folder#"~/"}" ;;
  /*) ;;
  *) folder="${CLAUDE_PROJECT_DIR:-$PWD}/$folder" ;;
esac
f="$folder/.ahc/estop.json"
[ -e "$f" ] || exit 0
# AHC's own data folder (AHC_HOME, default ~/.ahc) is no task's: no server works there.
home="${AHC_HOME:-$HOME/.ahc}"
case "$home" in "~") home="$HOME" ;; "~/"*) home="$HOME/${home#"~/"}" ;; esac
[ "$folder/.ahc" -ef "$home" ] && exit 0
# Read as the server and ahc-estop read it (src/ahc/workspace/estop.py): only a JSON object whose
# "engaged" is false releases; anything else counts as pressed. Without a usable python3, the text rule.
/usr/bin/env python3 -I -c '
import json, sys
try:
  with open(sys.argv[1], "rb") as f:
    data = json.loads(f.read())
except FileNotFoundError:
  sys.exit(0)
except Exception:
  sys.exit(3)
sys.exit(0 if isinstance(data, dict) and data.get("engaged") is False else 3)
' "$f" 2>/dev/null
case $? in
  0) exit 0 ;;
  3) ;;
  *) grep -Eq '"engaged"[[:space:]]*:[[:space:]]*false' "$f" 2>/dev/null && exit 0 ;;
esac
# While the stop is engaged only what lets the agent look and tell the person runs: the names in
# estop-allowed-tools.txt (Read, Grep, Glob, ToolSearch, TodoWrite, SendMessage, ListAgents and AHC's read-only tools, capture,
# open_dashboard and report_decision). Everything else is denied: Bash, edits, fetches, subagents, AHC's actions and
# person tools, other MCP servers' tools, and any tool not listed. Without python3, or without the list, every call is denied.
/usr/bin/env python3 -I -c '
import json, sys
try:
  tool = json.load(sys.stdin).get("tool_name")
  with open(sys.argv[1], encoding="utf-8") as f:
    allowed = {line.strip() for line in f if line.strip() and not line.startswith("#")}
  sys.exit(0 if isinstance(tool, str) and tool in allowed else 1)
except Exception:
  sys.exit(1)
' "$(dirname "$0")/estop-allowed-tools.txt" 2>/dev/null && exit 0
# Deny this call (and any others sent with it in parallel), then end the turn.
printf '%s\n' '{"continue": false, "stopReason": "AHC emergency stop engaged in this task folder: the agent is stopped. A person releases it on the run dashboard or with ahc-estop --release; the deck is checked again before anything moves.", "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "AHC emergency stop engaged: no tool runs until a person releases it."}}'
