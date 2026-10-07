"""The Claude Code hook that feeds the dashboard live: what was asked, said, called, and when it ended."""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "agent-feed.sh"
AHC = "mcp__plugin_ahc_ahc__"


def transcript(path: Path, records: list[dict]) -> Path:
  path.write_text("".join(json.dumps(r) + "\n" for r in records))
  return path


def human(uid, text):
  return {"type": "user", "uuid": uid, "sessionId": "s1", "timestamp": "2026-10-02T03:00:00.000Z",
          "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def assistant(uid, blocks):
  return {"type": "assistant", "uuid": uid, "sessionId": "s1", "timestamp": "2026-10-02T03:00:05.000Z",
          "message": {"role": "assistant", "content": blocks}}


def run(task: Path, event: str, tool: str, tool_input: dict, tx: Path, tmp: Path, **env) -> None:
  payload = {"session_id": "s1", "transcript_path": str(tx), "cwd": str(task), "tool_name": tool,
             "tool_input": tool_input, "hook_event_name": "PreToolUse" if event == "pre" else "Stop"}
  subprocess.run(["sh", str(HOOK), event], input=json.dumps(payload), text=True, check=True,
                 env={**os.environ, "CLAUDE_PROJECT_DIR": str(task), **env})


def feed(task: Path) -> list[dict]:
  path = task / ".ahc" / "agent-feed.jsonl"
  return [json.loads(l) for l in path.read_text().splitlines()] if path.is_file() else []


def test_the_feed_follows_the_agent(tmp_path):
  task = tmp_path / "task"
  (task / ".ahc").mkdir(parents=True)
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Run a 2-fold dilution on the OT-2"),
                                         assistant("a1", [{"type": "text", "text": "Starting the simulator first."}]),
                                         assistant("a2", [{"type": "tool_use", "name": AHC + "lab_overview", "input": {}}])])
  run(task, "pre", AHC + "aspirate", {"device": "ot2.right", "targets": ["plate:A1:H1"], "volumes": 100}, tx, tmp_path)
  kinds = [(e["kind"], e.get("text") or e.get("tool")) for e in feed(task)]
  assert kinds == [("request", "Run a 2-fold dilution on the OT-2"), ("said", "Starting the simulator first."),
                   ("doing", "aspirate")]
  assert feed(task)[2]["brief"] == "ot2.right · plate:A1:H1 · 100"
  assert feed(task)[0]["t"] != "2026-10-02T03:00:00"  # converted from UTC to local time
  run(task, "pre", "Bash", {"command": "export SECRET=1 && ahc-sim ot2"}, tx, tmp_path)
  last = feed(task)[-1]
  assert last["kind"] == "doing" and last["tool"] == "Bash" and last["brief"] == ""  # no shell command recorded
  assert len(feed(task)) == 4  # nothing said since: only the call
  transcript(tx, [human("u1", "Run a 2-fold dilution on the OT-2"),
                  assistant("a9", [{"type": "text", "text": "Done: columns 1-7 hold the series.\nDetails..."}])])
  run(task, "post", "Bash", {}, tx, tmp_path)  # after a call: the text it came with, no new call entry
  assert [e["kind"] for e in feed(task)][-1] == "said" and feed(task)[-1]["text"].startswith("Done")
  run(task, "stop", "", {}, tx, tmp_path, )
  assert feed(task)[-1]["kind"] == "finished" and feed(task)[-1]["text"].startswith("Done")


def test_the_final_answer_comes_from_the_stop_input(tmp_path):
  task = tmp_path / "task"
  (task / ".ahc").mkdir(parents=True)
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute"), assistant("a1", [{"type": "text", "text": "Mixing now."}])])
  payload = {"session_id": "s1", "transcript_path": str(tx), "last_assistant_message": "All 96 wells hold 100 uL."}
  subprocess.run(["sh", str(HOOK), "stop"], input=json.dumps(payload), text=True, check=True,
                 env={**os.environ, "CLAUDE_PROJECT_DIR": str(task)})
  assert feed(task)[-1] == {**feed(task)[-1], "kind": "finished", "text": "All 96 wells hold 100 uL."}


def test_the_first_ahc_call_starts_the_feed(tmp_path):
  task = tmp_path / "task"
  task.mkdir()
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute on the STARlet"),
                                         assistant("a1", [{"type": "text", "text": "Loading the AHC tools."}])])
  run(task, "pre", "ToolSearch", {"query": "ahc"}, tx, tmp_path)
  assert not (task / ".ahc").exists()  # not a lab task yet
  run(task, "pre", AHC + "lab_overview", {}, tx, tmp_path)  # before the server writes anything
  assert [(e["kind"], e.get("text") or e.get("tool")) for e in feed(task)] == [
    ("request", "Dilute on the STARlet"), ("said", "Loading the AHC tools."), ("doing", "lab_overview")]


def test_other_folders_and_the_switch_cost_nothing(tmp_path):
  task = tmp_path / "plain-project"
  task.mkdir()
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Fix the bug")])
  run(task, "pre", "Bash", {"command": "ls"}, tx, tmp_path)
  run(task, "stop", "", {}, tx, tmp_path)
  assert not (task / ".ahc").exists()
  (task / ".ahc").mkdir()
  run(task, "pre", AHC + "lab_overview", {}, tx, tmp_path, AHC_FEED="0")
  assert feed(task) == []
