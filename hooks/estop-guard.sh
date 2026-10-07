#!/bin/sh
# AHC emergency stop, Claude Code side: before every tool call, if this task folder's stop is
# engaged, end the agent's turn (all tools, not only AHC's). The MCP server refuses AHC actions on
# its own; this stops the rest of the agent too. A no-op, in a few milliseconds, everywhere else.
f="${CLAUDE_PROJECT_DIR:-$PWD}/.ahc/estop.json"
[ -f "$f" ] || exit 0
grep -q '"engaged": *true' "$f" || exit 0
# Deny this call (and any others sent with it in parallel), then end the turn.
printf '%s\n' '{"continue": false, "stopReason": "AHC emergency stop engaged in this task folder: the agent is stopped. A person releases it on the run dashboard or with ahc-estop --release; the deck is checked again before anything moves.", "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "AHC emergency stop engaged: no tool runs until a person releases it."}}'
