"""AHC live agent feed for Claude Code: what was asked, what the agent says, what it calls, when it is done.

Called by hooks/agent-feed.sh before every tool call ("pre", which records the call), after it
("post": Claude Code writes the agent's text for a call to the transcript only around then, so this
catches it without waiting for the next call) and when the agent finishes a turn ("stop"). It reads the
tail of the session transcript Claude Code passes in and appends what is new to
`<task>/.ahc/agent-feed.jsonl`, which the run dashboard shows live. A folder becomes an AHC task with
the agent's first AHC tool call: from then on (with what was asked and said before it) the feed is
written, before the server itself writes anything. Only tool names are recorded for tools other than
AHC's, so shell commands and file contents never land in the feed.

A call made while the folder's emergency stop is engaged is recorded as denied, not as being called:
the stop hook (estop-guard.sh) refuses it (all but a capture, open_dashboard and the ToolSearch that loads them), by the same reading of .ahc/estop.json. Parallel tool calls
run this at once; the feed is locked while one of them reads what it holds and appends, and every call
carries its tool_use_id, so nothing is written twice.

Standard library only, and Python 3.9: this runs with the system python3, outside the plugin's
environment. AHC_FEED=0 turns it off.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

AHC_TOOL = "mcp__plugin_ahc_ahc__"
RUN_WHILE_STOPPED = (AHC_TOOL + "capture", AHC_TOOL + "open_dashboard", "ToolSearch")  # what estop-guard.sh lets through a stop
TAIL_BYTES = 400_000
FEED_TAIL_BYTES = 2_000_000
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


def new_entries(event: str, data: dict, seen: set[str], stopped: bool = False) -> list[dict]:
  """What this hook call adds: the request and agent text not seen before, then the call (denied while
  the emergency stop is engaged) or the end."""
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
    call = {"kind": "denied" if stopped else "doing", "tool": _short(tool),
            "brief": _brief(tool, data.get("tool_input") or {}), "ahc": tool.startswith(AHC_TOOL), "t": _now()}
    if data.get("tool_use_id"):
      call["id"] = f"call:{data['tool_use_id']}"
    out.append(call)
  elif event == "stop":  # the turn ended: the answer comes with the hook input, else from the transcript
    answer = data.get("last_assistant_message")
    if isinstance(answer, str) and answer.strip():
      bid, text = f"{session}:{hashlib.sha256(answer.encode()).hexdigest()[:12]}", answer.strip()
    else:
      bid, text = last_text if last_text is not None else (f"{session}:{time.time():.3f}", "")
    out.append({"kind": "finished", "text": text[:TEXT_LIMIT], "id": "final:" + bid, "t": _now()})
  for e in out:
    e["session"] = session
  return [e for e in out if e.get("id") not in seen]


def _seen(text: str) -> set[str]:
  seen: set[str] = set()
  for line in text.splitlines()[-2000:]:
    try:
      entry = json.loads(line)
    except ValueError:
      continue
    if isinstance(entry, dict) and entry.get("id"):
      seen.add(entry["id"])
  return seen


def estop_engaged(ahc_dir: Path) -> bool:
  """The folder's emergency stop, read as estop-guard.sh and the server read it: no file is no stop, a
  JSON object whose "engaged" is false is a release, anything else counts as pressed."""
  try:
    data = json.loads((ahc_dir / "estop.json").read_bytes())
  except FileNotFoundError:
    return False
  except Exception:  # noqa: BLE001 - unreadable or not JSON: pressed
    return True
  return not (isinstance(data, dict) and data.get("engaged") is False)


def _lock(f) -> None:
  """Hold the feed while this call reads what it holds and appends; give up after 2 s rather than hold
  the agent up (a duplicate line is the worst that can happen then)."""
  try:
    import fcntl
  except ImportError:
    return
  deadline = time.time() + 2
  while True:
    try:
      fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
      return
    except OSError:
      if time.time() > deadline:
        return
      time.sleep(0.01)


def task_folder(data: dict) -> Path:
  """The task folder, as the server and the shell hooks resolve it: AHC_WORKSPACE (a relative one from
  the project folder), else the project folder Claude Code started in."""
  project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
  chosen = os.environ.get("AHC_WORKSPACE")
  if not chosen:
    return project
  folder = Path(chosen).expanduser()
  return folder if folder.is_absolute() else project / folder


def main() -> None:
  if os.environ.get("AHC_FEED", "1") == "0":
    return
  event = sys.argv[1] if len(sys.argv) > 1 else "pre"
  try:
    data = json.loads(sys.stdin.read() or "{}")
  except ValueError:
    return
  ahc_dir = task_folder(data) / ".ahc"
  feed = ahc_dir / "agent-feed.jsonl"
  tool = data.get("tool_name") or ""
  if ahc_dir.resolve() == Path(os.environ.get("AHC_HOME") or "~/.ahc").expanduser().resolve():
    return  # AHC's own data folder (AHC_HOME), not a task's: a session in the home folder is no AHC task
  if not ahc_dir.is_dir() and not tool.startswith(AHC_TOOL):
    return  # not an AHC task (yet): nothing to watch
  stopped = event == "pre" and tool not in RUN_WHILE_STOPPED and estop_engaged(ahc_dir)
  ahc_dir.mkdir(exist_ok=True)  # the first AHC call: the dashboard can show the agent from here on
  with open(feed, "a+b") as f:
    _lock(f)
    f.seek(0, os.SEEK_END)
    f.seek(max(0, f.tell() - FEED_TAIL_BYTES))
    entries = new_entries(event, data, _seen(f.read().decode("utf-8", errors="replace")), stopped)
    f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode("utf-8"))


if __name__ == "__main__":
  try:
    main()
  except Exception:  # noqa: BLE001 - a watcher must never get in the agent's way
    pass
