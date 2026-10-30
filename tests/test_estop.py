"""The emergency stop: pressed anywhere, the server stops at once and refuses everything until a person
releases it; the Claude Code hook ends the agent's turn. Every reader reads the stop file the same,
fail-safe way, and the stop controls say whether a server took the stop."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from ahc.examples import STARLET_LAYOUT
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from conftest import load_verified
from hookenv import HOOKS, run_hook

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
AGENT_VARS = ("CLAUDECODE", "AI_AGENT")


async def test_a_pressed_stop_refuses_every_action_but_not_reading(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    estop.engage(Workspace(tmp_path), by="test")
    for tool, args in (("pick_up_tips", {"device": DEV}), ("load_layout", {"layout": STARLET_LAYOUT}),
                       ("verify", {"check": "deck_matches_layout"}),
                       ("configure_devices", {"devices": [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]})):
      assert await lab.refused(tool, **args) == "emergency_stop", tool
    overview = await lab.call("lab_overview")
    assert overview["emergency_stop"]["engaged"] and "emergency stop" in overview["next"]
    await lab.call("get_state")
    await lab.call("report_decision", decision="Stopped; waiting for the person", waiting_for="person")


async def test_the_stop_interrupts_the_running_command(tmp_path, open_lab, monkeypatch):
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)

    async def slow_aspirate(*args, **kwargs):  # a long move on the device
      await asyncio.sleep(5)

    monkeypatch.setattr(lab.server.lab.adapter, "aspirate", slow_aspirate)
    started = time.monotonic()
    async with anyio.create_task_group() as tg:
      async def press_soon():
        await anyio.sleep(0.3)
        estop.engage(Workspace(tmp_path), by="test")
      tg.start_soon(press_soon)
      text = await lab.call_text("aspirate", device=DEV, targets=["diluent"], volumes=50)
    assert "[emergency_stop]" in text and "interrupted aspirate" in text
    assert time.monotonic() - started < 2  # not the 5 s the command would have taken
    entries = lab.server.lab.runlog.entries
    assert [e for e in entries if e.get("type") == "estop"][0]["interrupted"] is True
    assert [e for e in entries if e.get("op") == "aspirate"][-1]["error"] == "emergency_stop"


async def test_only_a_release_record_releases_it(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device=DEV)  # tips are on when the stop comes
    estop.engage(ws, by="test")
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    estop.path(ws).unlink()  # deleting the file is not a release: the server puts it back
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    assert estop.read(ws)["engaged"]
    estop.release(ws, by="person")
    # Released: the deck is checked again first; with tips on, that is the way to go on, not a reload.
    assert await lab.refused("drop_tips", device=DEV) == "gate_pending"
    await lab.call("verify", check="deck_matches_layout")
    await lab.call("drop_tips", device=DEV)
  states = [e["state"] for e in lab.server.lab.runlog.entries if e.get("type") == "estop"]
  assert states == ["engaged", "released"]


@pytest.mark.parametrize("record", [{"engaged": False, "id": "elsewhere", "released_by": "someone"},
                                    {"engaged": False}], ids=["another-stop", "no-id"])
async def test_a_release_record_of_another_stop_puts_the_held_one_back(tmp_path, open_lab, record):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    held = estop.engage(ws, by="test")
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    # Written by hand, or a press and a release that both landed between two of the server's polls.
    estop._write(ws, record)
    assert await lab.refused("drop_tips", device=DEV) == "emergency_stop"
    now = estop.read(ws)  # what the file, ahc-estop --status, the dashboard and the hooks read
    assert now["engaged"] is True and now["id"] == held["id"]
    audit = [e for e in lab.server.lab.runlog.entries if e.get("event") == "estop_release_mismatch"]
    assert [(e["id"], e["released_id"]) for e in audit] == [(held["id"], record.get("id"))]
    assert "releases it once more" in audit[0]["detail"]
    estop.release(ws, by="person")  # the person releases again: the stop the server holds
    assert await lab.refused("drop_tips", device=DEV) == "gate_pending"
  states = [(e["state"], e["id"]) for e in lab.server.lab.runlog.entries if e.get("type") == "estop"]
  assert states == [("engaged", held["id"]), ("released", held["id"])]


# What anyone might write into .ahc/estop.json, and whether it is a stop. Only a JSON object whose
# "engaged" is false releases; everything else counts as pressed.
STOP_FILES = [
  (json.dumps({"engaged": True, "id": "a1b2c3d4", "at": "2026-10-22T10:00:00", "by": "x"}, indent=2), True),
  ('{"engaged":true}', True),
  ('{"engaged" : true}', True),
  ('{"engaged":\n  true}', True),
  ('{"engaged":\ttrue}', True),
  ('{"engaged": 1}', True),
  ('{"engaged": "yes"}', True),
  ('{"engaged": 0}', True),
  ('{"engaged": null}', True),
  ("{}", True),
  ("", True),
  ("stop", True),
  ('{"engaged": true}  garbage', True),
  ('{"engaged": false,}', True),
  ('{"engaged": false', True),
  ('[{"engaged": false}]', True),
  ('"engaged": false', True),
  (json.dumps({"engaged": False, "id": "a1b2c3d4", "released_by": "x"}, indent=2), False),
  ('{"engaged":false}', False),
  ('{"engaged":\n  false, "id": "x"}', False),
]


def _feed_hook_module():
  spec = importlib.util.spec_from_file_location("agent_feed", HOOKS / "agent_feed.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.mark.parametrize("text,engaged", STOP_FILES)
def test_every_reader_reads_the_stop_file_the_same_way(tmp_path, text, engaged):
  ws = Workspace(tmp_path)
  ws.dir.mkdir()
  estop.path(ws).write_text(text)
  record = estop.read(ws)
  assert record["engaged"] is engaged  # the server, the dashboard and ahc-estop
  hook = run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(tmp_path))
  assert hook.returncode == 0 and bool(hook.stdout) is engaged, hook.stderr  # the Claude Code hook
  assert _feed_hook_module().estop_engaged(ws.dir) is engaged  # the live feed: a refused call shows as denied
  if engaged:
    assert json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert record["id"] and record["at"] and record["by"]  # something to latch and to release


def test_the_hook_falls_back_to_the_text_rule_without_python(tmp_path):
  ws = Workspace(tmp_path / "task")
  record = estop.engage(ws, by="test")
  tools = tmp_path / "bin"  # grep, and no python3 for /usr/bin/env to find
  tools.mkdir()
  grep = next(p for p in ("/usr/bin/grep", "/bin/grep") if Path(p).exists())
  (tools / "grep").symlink_to(grep)
  without_python = {"PATH": str(tools), "CLAUDE_PROJECT_DIR": str(ws.root)}
  assert run_hook("estop-guard.sh", **without_python).stdout
  estop.release(ws, by="person")
  assert run_hook("estop-guard.sh", **without_python).stdout == ""
  estop.path(ws).write_text("stop")
  assert run_hook("estop-guard.sh", **without_python).stdout
  assert record["engaged"]


async def test_a_stop_file_ahc_did_not_write_stops_and_releases_like_any_other(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    ws.dir.mkdir(exist_ok=True)
    estop.path(ws).write_text("stop\n")  # `echo stop > .ahc/estop.json`
    assert await lab.refused("pick_up_tips", device=DEV) == "emergency_stop"
    pressed = estop.read(ws)
    assert pressed["unreadable"] == "not JSON" and pressed["id"].startswith("file-")
    assert estop.engage(ws, by="dashboard") == pressed  # already stopped: kept as it is
    released = estop.release(ws, by="person")
    assert released["engaged"] is False and released["id"] == pressed["id"] and "unreadable" not in released
    assert await lab.refused("pick_up_tips", device=DEV) == "gate_pending"  # released: the deck is checked again
  estops = [e for e in lab.server.lab.runlog.entries if e.get("type") == "estop"]
  assert [e["state"] for e in estops] == ["engaged", "released"]


def test_releasing_nothing_says_so(tmp_path):
  ws = Workspace(tmp_path)
  assert estop.release(ws, by="person") is None  # no file
  estop.engage(ws, by="test")
  assert estop.release(ws, by="person")["engaged"] is False
  assert estop.release(ws, by="person") is None  # already a release record: nothing written
  out = _cli("--release", "--workspace", str(tmp_path))
  assert out.returncode != 0 and "nothing to release" in out.stderr and "Released" not in out.stdout


def _cli(*args: str, env: dict | None = None, tty_input: str | None = None) -> subprocess.CompletedProcess:
  """ahc-estop as a person (on a terminal, `tty_input` typed) or a script would run it."""
  cli = [str(Path(sys.executable).parent / "ahc-estop"), *args]
  env = {k: v for k, v in os.environ.items() if k not in AGENT_VARS} if env is None else env
  if tty_input is None:
    return subprocess.run(cli, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)
  controller, terminal = os.openpty()
  try:
    proc = subprocess.Popen(cli, stdin=terminal, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    os.write(controller, tty_input.encode())
    out, err = proc.communicate(timeout=60)
  finally:
    os.close(controller)
    os.close(terminal)
  return subprocess.CompletedProcess(cli, proc.returncode, out, err)


def test_releasing_from_a_shell_without_a_person_is_refused(tmp_path):
  estop.engage(Workspace(tmp_path), by="test")
  out = _cli("--release", "--workspace", str(tmp_path))
  assert out.returncode != 0 and "person at a terminal" in out.stderr
  assert estop.read(Workspace(tmp_path))["engaged"]


@pytest.mark.parametrize("agent", AGENT_VARS)
def test_an_agent_cannot_release_it_even_through_a_terminal(tmp_path, agent):
  estop.engage(Workspace(tmp_path), by="test")
  env = {**{k: v for k, v in os.environ.items() if k not in AGENT_VARS}, agent: "1"}
  out = _cli("--release", "--workspace", str(tmp_path), env=env, tty_input="release\n")  # a pty passes isatty
  assert out.returncode != 0 and "not from an agent" in out.stderr and agent in out.stderr
  assert estop.read(Workspace(tmp_path))["engaged"]


def test_the_person_releases_it_at_a_terminal(tmp_path):
  ws = Workspace(tmp_path)
  pressed = estop.engage(ws, by="test")
  out = _cli("--release", "--workspace", str(tmp_path), tty_input="release\n")
  assert out.returncode == 0 and "Released" in out.stdout, out.stderr
  record = estop.read(ws)
  assert record["engaged"] is False and record["id"] == pressed["id"] and record["released_by"] == "terminal"


def test_the_stop_never_lands_in_ahcs_own_data_folder(tmp_path):
  home = tmp_path / "home"
  (home / ".ahc" / "sim").mkdir(parents=True)
  env = {**{k: v for k, v in os.environ.items() if k not in AGENT_VARS}, "AHC_HOME": str(home / ".ahc")}
  out = _cli("--workspace", str(home), env=env)
  assert out.returncode != 0 and "AHC's own data folder" in out.stderr
  assert not (home / ".ahc" / "estop.json").exists()
  estop.engage(Workspace(home), by="an older ahc-estop")  # a stop file left there earlier
  assert run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(home), AHC_HOME=str(home / ".ahc")).stdout == ""


def test_the_claude_code_hook_ends_the_turn_only_when_engaged(tmp_path):
  run = lambda: run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(tmp_path))
  assert run().stdout == ""  # no task here: a no-op
  estop.engage(Workspace(tmp_path), by="test")
  out = json.loads(run().stdout)
  assert out["continue"] is False and "emergency stop" in out["stopReason"]
  assert out["hookSpecificOutput"]["permissionDecision"] == "deny"  # parallel calls in the batch do not run either
  estop.release(Workspace(tmp_path), by="person")
  assert run().stdout == ""


def test_the_hook_watches_the_servers_folder(tmp_path):
  """The server's folder is --workspace, else AHC_WORKSPACE, else the folder it started in (the project
  folder): the hook follows AHC_WORKSPACE the same way, a relative one from the project folder."""
  project, task = tmp_path / "project", tmp_path / "project" / "runs" / "task"
  task.mkdir(parents=True)
  estop.engage(Workspace(task), by="test")
  assert run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(project)).stdout == ""  # not the server's folder
  for chosen in (str(task), "runs/task"):
    assert run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(project), AHC_WORKSPACE=chosen).stdout, chosen
  home_task = tmp_path / "home" / "task"
  home_task.mkdir(parents=True)
  estop.engage(Workspace(home_task), by="test")
  assert run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(project), HOME=str(tmp_path / "home"),
                  AHC_WORKSPACE="~/task").stdout
  estop.engage(Workspace(project), by="test")
  estop.release(Workspace(task), by="person")
  assert run_hook("estop-guard.sh", CLAUDE_PROJECT_DIR=str(project), AHC_WORKSPACE=str(task)).stdout == ""


def _run_log(ws: Workspace, *entries: dict) -> Path:
  ws.runs_dir.mkdir(parents=True, exist_ok=True)
  log = ws.runs_dir / f"r{len(list(ws.runs_dir.iterdir()))}.jsonl"
  log.write_text("".join(json.dumps(e) + "\n" for e in [{"type": "run", "run_id": log.stem}, *entries]))
  return log


def test_whether_a_server_took_the_stop_comes_from_its_run_log(tmp_path):
  ws = Workspace(tmp_path)
  pressed = estop.engage(ws, by="dashboard")
  assert estop.acknowledgement(ws, pressed) is None  # no run log: no server took it
  _run_log(ws, {"type": "estop", "state": "engaged", "id": "another", "t": "2026-10-22T10:00:01"})
  assert estop.acknowledgement(ws, pressed) is None  # another stop
  _run_log(ws, {"type": "estop", "state": "engaged", "id": pressed["id"], "t": "2026-10-22T10:00:02"})
  ack = estop.acknowledgement(ws, pressed)
  assert ack["at"] == "2026-10-22T10:00:02" and ack["halt"] is None
  _run_log(ws, {"type": "estop", "state": "engaged", "id": pressed["id"], "t": "2026-10-22T10:00:02"},
           {"type": "audit", "event": "device_halt_failed", "detail": "timeout"})
  assert estop.acknowledgement(ws, pressed)["halt"] == "failed"  # the outcome that matters most is shown


def test_a_run_log_entry_without_an_id_is_matched_by_when_and_who(tmp_path):
  ws = Workspace(tmp_path)
  pressed = estop.engage(ws, by="terminal")
  _run_log(ws, {"type": "estop", "state": "engaged", "by": "terminal", "at": "1999-01-01T00:00:00"})
  assert estop.acknowledgement(ws, pressed) is None
  _run_log(ws, {"type": "estop", "state": "engaged", "by": "terminal", "at": pressed["at"], "t": pressed["at"]},
           {"type": "audit", "event": "device_halted", "detail": "the device stopped its run"})
  assert estop.acknowledgement(ws, pressed)["halt"] == "halted"


def test_a_stop_file_written_again_is_a_press_of_its_own(tmp_path):
  """A stop file AHC did not write keeps its id when the same text is written again; the server's entry for
  the earlier press must not count for the later one."""
  ws = Workspace(tmp_path)
  ws.dir.mkdir()
  estop.path(ws).write_text("stop\n")
  os.utime(estop.path(ws), (time.time() - 3600, time.time() - 3600))
  first = estop.read(ws)
  log = _run_log(ws, {"type": "estop", "state": "engaged", "id": first["id"], "at": first["at"], "by": first["by"],
                      "t": first["at"]},
                 {"type": "audit", "event": "device_not_halted", "detail": "nothing was running"},
                 {"type": "estop", "state": "released", "id": first["id"]})
  estop.path(ws).write_text("stop\n")  # pressed again, an hour later, the same way
  again = estop.read(ws)
  assert again["id"] == first["id"] and again["at"] != first["at"]
  assert estop.acknowledgement(ws, again) is None  # the earlier press's entry is not this one's
  with log.open("a") as f:
    f.write(json.dumps({"type": "estop", "state": "engaged", "id": again["id"], "at": again["at"], "t": again["at"]})
            + "\n")
  ack = estop.acknowledgement(ws, again)
  assert ack["at"] == again["at"] and ack["halt"] is None


async def test_ahc_estop_says_whether_a_server_took_the_stop(tmp_path, open_lab):
  nobody = tmp_path / "nobody"
  nobody.mkdir()
  out = _cli("--workspace", str(nobody), "--wait", "0")
  assert out.returncode == 0 and f"engaged for {nobody}" in out.stdout
  assert "No AHC server has worked in this folder" in out.stdout and "Not acknowledged by any AHC server" in out.stdout
  task = tmp_path / "task"
  task.mkdir()
  async with open_lab(task, "sim") as lab:
    await load_verified(lab, layout=STARLET_LAYOUT)
    out = await anyio.to_thread.run_sync(lambda: _cli("--workspace", str(task), "--wait", "10"))
    assert "Acknowledged by the AHC server" in out.stdout, out.stdout
    status = json.loads(_cli("--status", "--workspace", str(task)).stdout)
  assert status["engaged"] and status["folder"] == str(task) and status["acknowledged"]["at"]


@pytest.mark.parametrize("tool,runs", [("mcp__plugin_ahc_ahc__capture", True), ("mcp__plugin_ahc_ahc__open_dashboard", True), ("ToolSearch", True),
                                       ("mcp__plugin_ahc_ahc__aspirate", False), ("Bash", False), (None, False)])
def test_a_capture_still_runs_while_the_stop_is_engaged(tmp_path, tool, runs):
  ws = Workspace(tmp_path)
  estop.engage(ws, by="test")
  stdin = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool}) if tool else ""
  hook = run_hook("estop-guard.sh", stdin=stdin, CLAUDE_PROJECT_DIR=str(tmp_path))
  assert hook.returncode == 0, hook.stderr
  assert (hook.stdout == "") is runs  # the cameras record the deck; anything else is denied and ends the turn

