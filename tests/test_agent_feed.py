"""The Claude Code hook that feeds the dashboard live: what was asked, said, called, and when it ended.
Run as Claude Code runs it: /bin/sh, and the system python3 (hookenv)."""

import json
import subprocess
from pathlib import Path

from hookenv import HAS_SYSTEM_PYTHON, HOOKS, SYSTEM_PYTHON, hook_env

HOOK = HOOKS / "agent-feed.sh"
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
  """One hook event as Claude Code sends it. A tool call fires the hook for every tool ('pre') and, for an
  AHC tool, the AHC tools' own one ('start') too."""
  payload = {"session_id": "s1", "transcript_path": str(tx), "cwd": str(task), "tool_name": tool,
             "tool_input": tool_input, "hook_event_name": "PreToolUse" if event == "pre" else "Stop"}
  events = [event] + (["start"] if event == "pre" and tool.startswith(AHC) else [])
  for e in events:
    subprocess.run(["/bin/sh", str(HOOK), e], input=json.dumps(payload), text=True, check=True, timeout=30,
                   env=hook_env(**{"CLAUDE_PROJECT_DIR": str(task), **env}))


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
  subprocess.run(["/bin/sh", str(HOOK), "stop"], input=json.dumps(payload), text=True, check=True,
                 env=hook_env(CLAUDE_PROJECT_DIR=str(task)))
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
  run(task, "pre", AHC + "get_state", {}, tx, tmp_path)  # a task now: recorded once, by the hook for every tool
  assert [e.get("tool") for e in feed(task) if e["kind"] == "doing"] == ["lab_overview", "get_state"]


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


def test_outside_a_task_the_hook_never_reads_its_input(tmp_path):
  """The folder decides before the input is read: a session elsewhere never waits on a large tool result."""
  task, plain = tmp_path / "task", tmp_path / "plain"
  (task / ".ahc").mkdir(parents=True)
  plain.mkdir()
  for folder, event in ((plain, "pre"), (plain, "post"), (plain, "stop"), (task, "start")):
    proc = subprocess.Popen(["/bin/sh", str(HOOK), event], stdin=subprocess.PIPE,
                            env=hook_env(CLAUDE_PROJECT_DIR=str(folder)))
    try:
      assert proc.wait(timeout=10) == 0, (folder.name, event)  # the input is never closed: it was not read
    finally:
      proc.kill()
      proc.stdin.close()
  assert not (plain / ".ahc").exists() and feed(task) == []


def test_the_feed_goes_to_the_servers_folder(tmp_path):
  """AHC_WORKSPACE (as the server reads it) names the task folder, else the project folder."""
  project = tmp_path / "project"
  task = project / "tasks" / "dilution"
  task.mkdir(parents=True)
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute in the task folder")])
  for chosen in (str(task), "tasks/dilution"):
    run(project, "pre", AHC + "lab_overview", {}, tx, tmp_path, AHC_WORKSPACE=chosen)
    assert not (project / ".ahc").exists()
  assert [e["kind"] for e in feed(task)] == ["request", "doing", "doing"]


def test_the_home_folder_is_no_task(tmp_path):
  home = tmp_path / "home"
  (home / ".ahc" / "sim").mkdir(parents=True)  # AHC's own data folder (~/.ahc holds the simulator base)
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Plan my week")])
  python = SYSTEM_PYTHON if HAS_SYSTEM_PYTHON else "python3"
  for env in ({"AHC_HOME": str(home / ".ahc")}, {"AHC_HOME": "", "HOME": str(home)}):
    run(home, "pre", "Bash", {"command": "ls"}, tx, tmp_path, **env)
    run(home, "pre", AHC + "lab_overview", {}, tx, tmp_path, **env)
    run(home, "stop", "", {}, tx, tmp_path, **env)
    payload = {"session_id": "s1", "transcript_path": str(tx), "tool_name": AHC + "lab_overview"}
    subprocess.run([python, str(HOOK.parent / "agent_feed.py"), "pre"], input=json.dumps(payload),
                   text=True, check=True, env=hook_env(CLAUDE_PROJECT_DIR=str(home), **env))
  assert not (home / ".ahc" / "agent-feed.jsonl").exists()


def _pre(task: Path, tx: Path, tool: str, call_id: str, event: str = "pre") -> subprocess.Popen:
  payload = {"session_id": "s1", "transcript_path": str(tx), "cwd": str(task), "tool_name": tool, "tool_input": {},
             "tool_use_id": call_id, "hook_event_name": "PreToolUse"}
  proc = subprocess.Popen(["/bin/sh", str(HOOK), event], stdin=subprocess.PIPE, text=True,
                          env=hook_env(CLAUDE_PROJECT_DIR=str(task)))
  proc.stdin.write(json.dumps(payload))
  proc.stdin.close()
  return proc


def test_a_call_the_stop_refuses_shows_as_denied(tmp_path):
  from ahc.workspace import estop
  from ahc.workspace.store import Workspace

  task = tmp_path / "task"
  (task / ".ahc").mkdir(parents=True)
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute")])
  estop.engage(Workspace(task), by="dashboard")
  assert _pre(task, tx, AHC + "aspirate", "toolu_1").wait(timeout=30) == 0
  estop.release(Workspace(task), by="person")
  assert _pre(task, tx, AHC + "verify", "toolu_2").wait(timeout=30) == 0
  calls = [(e["kind"], e["tool"], e["id"]) for e in feed(task) if e["kind"] != "request"]
  assert calls == [("denied", "aspirate", "call:toolu_1"), ("doing", "verify", "call:toolu_2")]


def test_the_feed_shows_a_call_as_denied_when_the_allowlist_does_not_list_it(tmp_path):
  """The feed reads the list the stop hook reads (hooks/estop-allowed-tools.txt): what it allows shows as doing, the
  rest as denied."""
  from ahc.workspace import estop
  from ahc.workspace.store import Workspace

  task = tmp_path / "task"
  (task / ".ahc").mkdir(parents=True)
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute")])
  estop.engage(Workspace(task), by="dashboard")
  tools = ["Read", AHC + "get_state", AHC + "report_decision", "Bash", "Write", AHC + "aspirate", AHC + "verify", "mcp__other__x"]
  for n, tool in enumerate(tools):
    assert _pre(task, tx, tool, f"toolu_{n}").wait(timeout=30) == 0
  kinds = {e["tool"]: e["kind"] for e in feed(task) if e["kind"] in ("doing", "denied")}
  assert kinds == {"Read": "doing", "get_state": "doing", "report_decision": "doing", "Bash": "denied", "Write": "denied",
                   "aspirate": "denied", "verify": "denied", "mcp__other__x": "denied"}


def test_parallel_calls_write_every_entry_once(tmp_path):
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute"), assistant("a1", [{"type": "text", "text": "Two reads at once."}])])
  for trial in range(5):  # Claude Code runs the hooks of parallel tool calls at the same time
    task = tmp_path / f"task{trial}"
    (task / ".ahc").mkdir(parents=True)
    procs = [_pre(task, tx, AHC + tool, f"toolu_{tool}") for tool in ("lab_overview", "find_layout_references", "get_state")]
    assert [p.wait(timeout=30) for p in procs] == [0, 0, 0]
    ids = [e.get("id") for e in feed(task)]
    assert len(ids) == len(set(ids)) == 5, feed(task)  # the request, the text, three calls


def test_the_first_ahc_call_is_recorded_once_whichever_hook_runs_first(tmp_path):
  task = tmp_path / "task"
  task.mkdir()
  tx = transcript(tmp_path / "t.jsonl", [human("u1", "Dilute")])
  assert _pre(task, tx, AHC + "lab_overview", "toolu_1", "start").wait(timeout=30) == 0  # starts the feed
  assert _pre(task, tx, AHC + "lab_overview", "toolu_1", "pre").wait(timeout=30) == 0  # then sees the task
  assert [e["kind"] for e in feed(task)] == ["request", "doing"]
