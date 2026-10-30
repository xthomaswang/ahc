#!/bin/sh
# AHC live agent feed (Claude Code): passes the hook input to agent_feed.py, but only in an AHC task
# (the folder has .ahc/). That is decided before the input is read, so every other session pays only
# this test. "start" comes from the AHC tools' own hook group: the first AHC call in a folder that is
# no task yet starts the feed; once it is one, the hook for every tool records the call instead.
[ "${AHC_FEED:-1}" = "0" ] && exit 0
# The task folder is the server's: AHC_WORKSPACE (as the server reads it), else the project folder.
folder="${AHC_WORKSPACE:-${CLAUDE_PROJECT_DIR:-$PWD}}"
case "$folder" in
  "~") folder="$HOME" ;;
  "~/"*) folder="$HOME/${folder#"~/"}" ;;
  /*) ;;
  *) folder="${CLAUDE_PROJECT_DIR:-$PWD}/$folder" ;;
esac
# AHC's own data folder (AHC_HOME, default ~/.ahc) is no task's .ahc: a session in the home folder is no task.
home="${AHC_HOME:-$HOME/.ahc}"
case "$home" in "~") home="$HOME" ;; "~/"*) home="$HOME/${home#"~/"}" ;; esac
[ "$folder/.ahc" -ef "$home" ] && exit 0
if [ "$1" = "start" ]; then
  [ -d "$folder/.ahc" ] && exit 0
  set -- pre
else
  [ -d "$folder/.ahc" ] || exit 0
fi
/usr/bin/env python3 "$(dirname "$0")/agent_feed.py" "$@" >/dev/null 2>&1
exit 0
