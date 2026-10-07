"""AHC live agent feed for Claude Code: what was asked, what the agent says, what it calls, when it is done.

Called by hooks/agent-feed.sh before every tool call ("pre", which records the call), after it
("post": Claude Code writes the agent's text for a call to the transcript only around then, so this
catches it without waiting for the next call) and when the agent finishes a turn ("stop"). It reads the tail of the session transcript Claude Code passes in and appends what is new
to `<task>/.ahc/agent-feed.jsonl`, which the run dashboard shows live. A folder becomes an AHC task
with the agent's first AHC tool call: from then on (with what was asked and said before it) the feed
is written, before the server itself writes anything. Only tool names are recorded for tools other
than AHC's, so shell commands and file contents never land in the feed. Standard library only: this
runs with the system python3, outside the plugin's environment. AHC_FEED=0 turns it off.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

AHC_TOOL = "mcp__plugin_ahc_ahc__"
TAIL_BYTES = 400_000
TEXT_LIMIT = 600
BRIEF_KEYS = ("check", "device", "targets", "volumes", "volume", "repetitions", "name", "decision", "op")


def _now() -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%S")


def _local(ts: str | None) -> str:
  """Transcript times are UTC ('...Z'); the run log and the dashboard use local time."""
  if not ts:
    return _now()
  try:
    from datetime import datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
  except ValueError:
    return ts[:19]


def _tail_records(transcript: str | None) -> list[dict]:
  if not transcript or not os.path.isfile(transcript):
    return []
  with open(transcript, "rb") as f:
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(max(0, size - TAIL_BYTES))
    lines = f.read().decode("utf-8", errors="replace").splitlines()
  if size > TAIL_BYTES:
    lines = lines[1:]  # the first line may be cut
  out = []
  for line in lines:
    try:
      out.append(json.loads(line))
    except ValueError:
      continue
  return out


def _brief(tool: str, args: dict) -> str:
  if not tool.startswith(AHC_TOOL):
    return ""  # never record other tools' inputs (shell commands, file contents)
  parts = []
  for key in BRIEF_KEYS:
    if key in args and args[key] is not None:
      value = args[key]
      parts.append(", ".join(map(str, value)) if isinstance(value, list) else str(value))
  return " · ".join(parts)[:200]


def _short(tool: str) -> str:
  return tool[len(AHC_TOOL):] if tool.startswith(AHC_TOOL) else tool


def new_entries(event: str, data: dict, seen: set[str]) -> list[dict]:
  """What this hook call adds: the request and agent text not seen before, then the call or the end."""
  session = data.get("session_id")
  records = _tail_records(data.get("transcript_path"))
  out: list[dict] = []
  last_text = None
  for r in records:
    if r.get("sessionId") not in (None, session) or r.get("isSidechain"):
      continue
    uid = r.get("uuid")
    content = (r.get("message") or {}).get("content")
    if r.get("type") == "user" and isinstance(content, str) and (r.get("origin") or {}).get("kind", "human") == "human":
      if uid and uid not in seen:
        out.append({"kind": "request", "text": content.strip()[:TEXT_LIMIT], "id": uid, "t": _local(r.get("timestamp"))})
    elif r.get("type") == "assistant" and isinstance(content, list):
      for k, block in enumerate(content):
        if block.get("type") == "text" and block.get("text", "").strip():
          bid = f"{uid}:{k}"
          last_text = (bid, block["text"].strip())
          if bid not in seen and event in ("pre", "post"):
            out.append({"kind": "said", "text": block["text"].strip()[:TEXT_LIMIT], "id": bid,
                        "t": _local(r.get("timestamp"))})
  if event == "pre":
    tool = data.get("tool_name") or ""
    out.append({"kind": "doing", "tool": _short(tool), "brief": _brief(tool, data.get("tool_input") or {}),
                "ahc": tool.startswith(AHC_TOOL), "t": _now()})
  elif event == "stop":  # the turn ended: the answer comes with the hook input, else from the transcript
    answer = data.get("last_assistant_message")
    if isinstance(answer, str) and answer.strip():
      bid, text = f"{session}:{hashlib.sha256(answer.encode()).hexdigest()[:12]}", answer.strip()
    else:
      bid, text = last_text if last_text is not None else (f"{session}:{time.time():.3f}", "")
    out.append({"kind": "finished", "text": text[:TEXT_LIMIT], "id": "final:" + bid, "t": _now()})
  for e in out:
    e["session"] = session
  return [e for e in out if e.get("kind") == "doing" or e.get("id") not in seen]


def _seen(path: Path) -> set[str]:
  seen: set[str] = set()
  if path.is_file():
    for line in path.read_text(errors="replace").splitlines()[-2000:]:
      try:
        entry = json.loads(line)
      except ValueError:
        continue
      if entry.get("id"):
        seen.add(entry["id"])
  return seen


def main() -> None:
  if os.environ.get("AHC_FEED", "1") == "0":
    return
  event = sys.argv[1] if len(sys.argv) > 1 else "pre"
  try:
    data = json.loads(sys.stdin.read() or "{}")
  except ValueError:
    return
  project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
  ahc_dir = project / ".ahc"
  feed = ahc_dir / "agent-feed.jsonl"
  tool = data.get("tool_name") or ""
  if not ahc_dir.is_dir() and not tool.startswith(AHC_TOOL):
    return  # not an AHC task (yet): nothing to watch
  entries = new_entries(event, data, _seen(feed))
  ahc_dir.mkdir(exist_ok=True)  # the first AHC call: the dashboard can show the agent from here on
  with feed.open("a") as f:
    for e in entries:
      f.write(json.dumps(e, ensure_ascii=False) + "\n")


if __name__ == "__main__":
  try:
    main()
  except Exception:  # noqa: BLE001 - a watcher must never get in the agent's way
    pass
