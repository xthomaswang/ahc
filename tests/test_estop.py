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

from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from ackenv import ROBOT, dead_pid, put_ack
from conftest import load_verified, robot_lab
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


async def until(condition, seconds: float = 10.0):
  with anyio.fail_after(seconds):
    while not condition():
      await anyio.sleep(0.05)


def acks_of(ws: Workspace, pressed: dict) -> dict[str, dict]:
  """The acknowledgements of this press by server."""
  return {a["server"]: a for a in estop.acknowledgements(ws, pressed)}


def answered(ws: Workspace, pressed: dict, n: int):
  return lambda: len(acks := estop.acknowledgements(ws, pressed)) == n and all(a["halt"] != "pending" for a in acks)


def test_an_ack_counts_only_for_the_press_it_answers(tmp_path):
  ws = Workspace(tmp_path)
  pressed = estop.engage(ws, by="dashboard")
  assert estop.acknowledgements(ws, pressed) == []  # no server wrote one
  put_ack(ws, {**pressed, "id": "another"}, os.getpid(), "stopped", ROBOT)
  put_ack(ws, {**pressed, "at": "1999-01-01T00:00:00"}, os.getpid(), "stopped", ROBOT, server="old001")  # the same stop file, pressed earlier
  assert estop.acknowledgements(ws, pressed) == []
  estop.acks_dir(ws, pressed["id"]).joinpath("junk.json").write_text("{not json")
  put_ack(ws, pressed, os.getpid(), "no_device")
  assert [a["halt"] for a in estop.acknowledgements(ws, pressed)] == ["no_device"]


def test_a_stop_file_written_again_is_a_press_of_its_own(tmp_path):
  """A stop file AHC did not write keeps its id when the same text is written again; the servers' acks of the
  earlier press must not count for the later one."""
  ws = Workspace(tmp_path)
  ws.dir.mkdir()
  estop.path(ws).write_text("stop\n")
  os.utime(estop.path(ws), (time.time() - 3600, time.time() - 3600))
  first = estop.read(ws)
  put_ack(ws, first, os.getpid(), "stopped", ROBOT)
  estop.path(ws).write_text("stop\n")  # pressed again, an hour later, the same way
  again = estop.read(ws)
  assert again["id"] == first["id"] and again["at"] != first["at"]
  assert estop.acknowledgements(ws, first) and estop.acknowledgements(ws, again) == []


def test_an_ack_from_a_dead_process_is_marked_stale(tmp_path):
  ws = Workspace(tmp_path)
  pressed = estop.engage(ws, by="terminal")
  put_ack(ws, pressed, os.getpid(), "no_device", server="live01")
  put_ack(ws, pressed, dead_pid(), "stopped", ROBOT, server="dead01", run_active=True)
  put_ack(ws, pressed, dead_pid(), "pending", ROBOT, server="dead02")
  acks = acks_of(ws, pressed)
  assert [acks[k]["stale"] for k in ("live01", "dead01", "dead02")] == [False, True, True]
  assert "STALE" in estop.describe_server(acks["dead01"]) and "STALE" not in estop.describe_server(acks["live01"])
  # A stale server that had finished says what it knew; one that died while stopping leaves the question open.
  assert estop.verdict([acks["live01"], acks["dead01"]], 1)["state"] == "stopped"
  open_question = estop.verdict(list(acks.values()), 1)
  assert open_question["state"] == "not_confirmed" and "ended before its device answered" in open_question["text"]
  assert estop.pid_alive(os.getpid()) and not estop.pid_alive(0) and not estop.pid_alive(None) and not estop.pid_alive("12")


@pytest.mark.parametrize("acks,state,words", [
  ([], "not_confirmed", "no AHC server has acknowledged the stop"),
  ([("no_device", None)], "nothing_to_stop", "no AHC server has a device run to stop"),
  ([("no_run", ROBOT)], "nothing_to_stop", "power switch"),
  ([("stopped", ROBOT)], "stopped", "the robot reports its run stopped"),
  ([("stopped", ROBOT), ("no_device", None), ("no_run", ROBOT)], "stopped", "the robot reports its run stopped"),
  ([("stopped", {**ROBOT, "backend": "sim"})], "stopped", "the simulator reports its run stopped"),
  ([("no_stop", {"id": "starlet", "model": "hamilton.starlet", "backend": "sim", "robot": None})], "nothing_to_stop", "no AHC server"),
  ([("no_stop", {"id": "starlet", "model": "hamilton.starlet", "backend": "robot", "robot": None})], "not_confirmed", "has no stop AHC can reach"),
  ([("stopped", ROBOT), ("not_confirmed", ROBOT)], "not_confirmed", "could not confirm the stop (timeout)"),
  ([("stopped", ROBOT), ("other_run_active", ROBOT)], "not_confirmed", "another client's run"),
  ([("stopped", ROBOT), ("pending", ROBOT)], "not_confirmed", "is still stopping"),
])
def test_the_verdict_never_says_stopped_while_a_question_is_open(tmp_path, acks, state, words):
  ws = Workspace(tmp_path)
  pressed = estop.engage(ws, by="test")
  for n, (halt, device) in enumerate(acks):
    put_ack(ws, pressed, os.getpid(), halt, device, server=f"s{n:05d}", detail="timeout")
  out = estop.verdict(estop.acknowledgements(ws, pressed), 2)
  assert out["state"] == state and words in out["text"]
  if state == "not_confirmed":
    assert out["text"].startswith("NOT confirmed: ") and out["text"].endswith("the power switch is the immediate stop")


async def test_every_server_writes_its_own_ack_bound_or_not(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)  # a run is active on the robot
    async with open_lab(tmp_path, backend=None) as idle, open_lab(tmp_path, backend=None) as another:
      pressed = estop.engage(ws, by="test")
      await until(answered(ws, pressed, 3))
      acks = acks_of(ws, pressed)
      assert len(acks) == 3 and {a["pid"] for a in acks.values()} == {os.getpid()}  # three servers in one process
      robot = acks[lab.server.lab._server]
      assert robot["halt"] == "stopped" and robot["run_active"] is True
      assert robot["device"] == {"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "robot": fake_robot.name}
      assert robot["id"] == pressed["id"] and robot["at"] == pressed["at"] and robot["started"] and robot["seen"] <= robot["updated"]
      for quiet in (idle, another):
        ack = acks[quiet.server.lab._server]
        assert ack["halt"] == "no_device" and ack["device"] is None and ack["run_active"] is None
      assert estop.verdict(list(acks.values()), 0) == {"state": "stopped", "text": "the robot reports its run stopped"}
      # The ahc-estop command shows one line per server and the same verdict.
      out = await anyio.to_thread.run_sync(lambda: _cli("--workspace", str(tmp_path), "--wait", "10"))
      assert out.returncode == 0 and "already engaged" in out.stdout, out.stderr
      lines = [line for line in out.stdout.splitlines() if line.startswith("  pid ")]
      assert len(lines) == 3 and sum(line.endswith(": no device bound: nothing to stop") for line in lines) == 2, out.stdout
      assert any(f"ot2 opentrons.ot2/robot '{fake_robot.name}', run active: " in line and "the robot reports its run stopped" in line for line in lines)
      assert out.stdout.rstrip().endswith("Verdict: the robot reports its run stopped.")


async def test_a_robot_that_does_not_take_the_stop_is_never_reported_stopped(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.stop_status = 500  # the robot does not take the stop
    pressed = estop.engage(ws, by="test")
    await until(answered(ws, pressed, 1))
    ack = acks_of(ws, pressed)[lab.server.lab._server]
    assert ack["halt"] == "not_confirmed" and ack["run_active"] is True and ack["detail"]
    out = await anyio.to_thread.run_sync(lambda: _cli("--status", "--workspace", str(tmp_path)))
    assert out.returncode == 0 and "Verdict: NOT confirmed: " in out.stdout, out.stdout
    assert "the power switch is the immediate stop." in out.stdout and "reports its run stopped" not in out.stdout


async def test_another_clients_run_is_reported_as_other_run_active(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
    fake_robot.other_client_run()  # the Opentrons App took the robot over
    pressed = estop.engage(ws, by="test")
    await until(answered(ws, pressed, 1))
    ack = acks_of(ws, pressed)[lab.server.lab._server]
    assert ack["halt"] == "other_run_active" and "replaced it" in ack["detail"]
    assert estop.verdict([ack], 0)["state"] == "not_confirmed"


async def test_a_stop_with_no_run_active_says_so(tmp_path, open_lab, fake_robot):
  ws = Workspace(tmp_path)
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await lab.call("lab_overview")  # bound to the robot, no layout loaded: no run
    pressed = estop.engage(ws, by="test")
    await until(answered(ws, pressed, 1))
    ack = acks_of(ws, pressed)[lab.server.lab._server]
    assert ack["halt"] == "no_run" and ack["run_active"] is False and ack["device"]["robot"] == fake_robot.name
    assert estop.verdict([ack], 0)["state"] == "nothing_to_stop"


async def test_a_simulated_starlet_has_no_device_stop_and_says_so(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, "sim") as lab, open_lab(tmp_path, backend=None) as idle:
    await load_verified(lab, layout=STARLET_LAYOUT)
    pressed = estop.engage(ws, by="test")
    await until(answered(ws, pressed, 2))
    acks = acks_of(ws, pressed)
    star = acks[lab.server.lab._server]
    assert star["halt"] == "no_stop" and star["device"]["model"] == "hamilton.starlet" and star["device"]["backend"] == "sim"
    assert acks[idle.server.lab._server]["halt"] == "no_device"
    assert estop.verdict(list(acks.values()), 0)["state"] == "nothing_to_stop"  # no real robot involved
    out = await anyio.to_thread.run_sync(lambda: _cli("--workspace", str(tmp_path), "--wait", "10"))
    assert "starlet hamilton.starlet/sim" in out.stdout and "Verdict: no AHC server has a device run to stop" in out.stdout


def test_ahc_estop_waits_for_a_pending_server_and_not_for_the_ones_with_nothing_bound(tmp_path):
  ws = Workspace(tmp_path)
  ws.dir.mkdir()
  pressed = estop.engage(ws, by="test")
  put_ack(ws, pressed, os.getpid(), "no_device", server="idle01")
  started = time.monotonic()
  acks = estop.wait_for_acks(ws, pressed, wait=30, settle=0.3)
  assert [a["halt"] for a in acks] == ["no_device"] and time.monotonic() - started < 3  # not the 30 s asked for
  put_ack(ws, pressed, os.getpid(), "pending", ROBOT, server="busy01")
  started = time.monotonic()
  acks = estop.wait_for_acks(ws, pressed, wait=1.0, settle=0.3)
  assert {a["halt"] for a in acks} == {"no_device", "pending"} and 0.9 < time.monotonic() - started < 3  # held up, to the end
  out = _cli("--workspace", str(tmp_path), "--wait", "0.5")
  assert "still stopping the device's run" in out.stdout and "Verdict: NOT confirmed: " in out.stdout
  put_ack(ws, pressed, dead_pid(), "stopped", ROBOT, server="gone01", run_active=True)
  put_ack(ws, pressed, os.getpid(), "stopped", ROBOT, server="busy01", run_active=True)
  out = _cli("--status", "--workspace", str(tmp_path))
  assert "STALE" in out.stdout and "Verdict: the robot reports its run stopped." in out.stdout, out.stdout
  data = json.loads(_cli("--status", "--json", "--workspace", str(tmp_path)).stdout)
  assert data["engaged"] and data["verdict"]["state"] == "stopped" and len(data["servers"]) == 3
  assert [s["stale"] for s in data["servers"] if s["server"] == "gone01"] == [True]


def test_ahc_estop_without_any_server_says_it_is_not_confirmed(tmp_path):
  nobody = tmp_path / "nobody"
  nobody.mkdir()
  out = _cli("--workspace", str(nobody), "--wait", "0")
  assert out.returncode == 0 and f"engaged for {nobody}" in out.stdout
  assert "No AHC server has worked in this folder" in out.stdout
  assert "Verdict: NOT confirmed: no AHC server has acknowledged the stop" in out.stdout
  assert "the power switch is the immediate stop." in out.stdout


def test_ahc_estop_status_without_a_stop(tmp_path):
  out = _cli("--status", "--workspace", str(tmp_path))
  assert out.returncode == 0 and f"No emergency stop is engaged for {tmp_path}" in out.stdout
  estop.release(Workspace(tmp_path), by="x")  # nothing engaged: nothing written
  estop.engage(Workspace(tmp_path), by="test")
  estop.release(Workspace(tmp_path), by="person")
  assert "released" in _cli("--status", "--workspace", str(tmp_path)).stdout
  assert json.loads(_cli("--status", "--json", "--workspace", str(tmp_path)).stdout)["engaged"] is False


AHC = "mcp__plugin_ahc_ahc__"
ALLOWED = ["Read", "Grep", "Glob", "ToolSearch", "TodoWrite", "SendMessage", "ListAgents",
           *(AHC + t for t in ("lab_overview", "get_state", "get_run", "list_checks", "describe_device", "get_params",
                               "find_layout_references", "find_robots", "capture", "physical_world_context", "open_dashboard",
                               "report_decision"))]
DENIED = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task", "BashOutput", "KillShell",
          *(AHC + t for t in ("load_layout", "pick_up_tips", "aspirate", "dispense", "mix", "drop_tips", "invoke", "verify",
                              "record_verdict", "confirm_config", "configure_devices", "disconnect", "map_camera",
                              "propose_verdict", "agent_verdict")),
          "mcp__other__read_file", "mcp__plugin_ahc_other__get_state", AHC + "an_unknown_tool", "read", "Read ", "Read\nBash", "", None]


@pytest.mark.parametrize("tool,runs", [(t, True) for t in ALLOWED] + [(t, False) for t in DENIED])
def test_while_stopped_only_what_lets_the_agent_look_and_report_runs(tmp_path, tool, runs):
  ws = Workspace(tmp_path)
  estop.engage(ws, by="test")
  stdin = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool}) if tool is not None else ""
  hook = run_hook("estop-guard.sh", stdin=stdin, CLAUDE_PROJECT_DIR=str(tmp_path))
  assert hook.returncode == 0, hook.stderr
  assert (hook.stdout == "") is runs  # an allowlist: anything not listed is denied, and the denial ends the turn
  if not runs:
    out = json.loads(hook.stdout)
    assert out["continue"] is False and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_allowlist_is_one_file_both_hooks_read():
  names = [line.strip() for line in (HOOKS / "estop-allowed-tools.txt").read_text().splitlines()
           if line.strip() and not line.startswith("#")]
  assert sorted(names) == sorted(ALLOWED) and len(names) == len(set(names))
  assert _feed_hook_module().run_while_stopped() == set(ALLOWED)
  for acting in ("aspirate", "load_layout", "verify", "record_verdict", "confirm_config", "disconnect"):
    assert AHC + acting not in names


def test_the_hook_denies_everything_without_python_or_without_the_list(tmp_path):
  ws = Workspace(tmp_path / "task")
  estop.engage(ws, by="test")
  read = json.dumps({"tool_name": "Read"})
  assert run_hook("estop-guard.sh", stdin=read, CLAUDE_PROJECT_DIR=str(ws.root)).stdout == ""
  tools = tmp_path / "bin"  # grep, and no python3 for /usr/bin/env to find
  tools.mkdir()
  grep = next(p for p in ("/usr/bin/grep", "/bin/grep") if Path(p).exists())
  (tools / "grep").symlink_to(grep)
  out = run_hook("estop-guard.sh", stdin=read, PATH=str(tools), CLAUDE_PROJECT_DIR=str(ws.root))
  assert json.loads(out.stdout)["continue"] is False  # even a Read: nothing says it may run
  copy = tmp_path / "hooks"  # the list missing next to the script
  copy.mkdir()
  (copy / "estop-guard.sh").write_text((HOOKS / "estop-guard.sh").read_text())
  out = subprocess.run(["/bin/sh", str(copy / "estop-guard.sh")], input=read, capture_output=True, text=True, timeout=30,
                       env={**os.environ, "CLAUDE_PROJECT_DIR": str(ws.root)})
  assert json.loads(out.stdout)["continue"] is False


def test_the_feed_hook_marks_a_call_denied_by_the_same_list(tmp_path):
  feed = _feed_hook_module()
  task = tmp_path / "task"
  (task / ".ahc").mkdir(parents=True)
  estop.engage(Workspace(task), by="test")
  seen = {}
  for tool in ALLOWED + DENIED:
    if not isinstance(tool, str) or not tool:
      continue
    stopped = tool not in feed.run_while_stopped() and feed.estop_engaged(task / ".ahc")
    seen[tool] = stopped
    hook = run_hook("estop-guard.sh", stdin=json.dumps({"tool_name": tool}), CLAUDE_PROJECT_DIR=str(task))
    assert stopped is bool(hook.stdout), tool  # the feed says denied exactly when the stop hook denies
  assert not any(seen[t] for t in ALLOWED) and all(seen[t] for t in DENIED if isinstance(t, str) and t)
