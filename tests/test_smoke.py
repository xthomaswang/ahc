"""ahc-smoke, rehearsed against a fake OT-2 robot-server (and an OT-2 simulator when one is named): a
whole stage with a person who answers yes, a person who says no, a second run in the same folder, task
limits the person added, and the terminal (not an agent) it insists on."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ahc.smoke import (HOMING, Refused, Smoke, Stop, ask_terminal, columns, deck_text, liquid_calls, plan_for,
                       refusal_text, smoke)
from ahc.viz.dashboard import replay, serve
from ahc.workspace.store import Workspace
from conftest import OT2_HOST, OT2_PORT, robot_server_up

pytestmark = pytest.mark.anyio
needs_simulator = pytest.mark.skipif(not robot_server_up(), reason="set AHC_TEST_OT2_PORT to a running OT-2 simulator")


def entries(folder: Path) -> list[dict]:
  log = sorted((folder / ".ahc" / "runs").glob("*.jsonl"))[-1]
  return [json.loads(line) for line in log.read_text().splitlines()]


def person(*answers: bool):
  """The person at the terminal, answering in turn; what they were asked is kept in `.asked`."""
  queue = list(answers)

  def ask(question: str) -> bool:
    ask.asked.append(question)
    return queue.pop(0) if queue else True
  ask.asked = []
  return ask


async def test_the_dilution_stage_end_to_end(tmp_path, fake_robot):
  ask, lines = person(), []
  code = await smoke(tmp_path, "dilution", "127.0.0.1", fake_robot.port, ask, out=lines.append, scripted=True)
  assert code == 0, "\n".join(lines)
  state = replay(sorted((tmp_path / ".ahc" / "runs").glob("*.jsonl"))[-1])
  agent = state["agent"]
  assert agent["plan"] == plan_for("dilution") and set(agent["steps"]) == {"done"} and agent["plan_done"]
  wells = state["plates"][0]["wells"]  # 100, 100, 200 uL; the dye at 1, 1/2, 1/4
  assert [wells[w]["v"] for w in ("A1", "A2", "A3")] == pytest.approx([100, 100, 200])
  assert [wells[w]["c"] for w in ("A1", "A2", "A3")] == pytest.approx([1, 0.5, 0.25])
  log_entries = entries(tmp_path)
  human = [e for e in log_entries if e.get("type") == "verification" and e.get("judged_by") == "human"]
  assert len(human) == 7 and all(e["verdict"] == "pass" for e in human)  # deck, 2 tip checks, 3 more, the plate
  assert len(ask.asked) == 9 and ask.asked[0].startswith("Confirm this robot") and ask.asked[1] == HOMING
  assert "task limits: none" in ask.asked[0]  # the person sees the whole config they confirm
  # Every liquid call names its height: 1.5 mm in the deep-well plate, 1 mm in the 360 uL plate.
  heights = [(e["op"], e["basic"]["targets"][0], e["effective"]["liquid_height_mm"]) for e in log_entries
             if e.get("op") in ("aspirate", "dispense", "mix") and e.get("status") == "ok"]
  assert len(heights) == 12
  for op, target, height in heights:
    assert height == (1.5 if op == "aspirate" and target in ("water", "stock") else 1.0), (op, target)
  device = [e for e in log_entries if e.get("type") == "device"][0]
  assert device["backend"] == "robot" and device["pipettes"]["right"]["name"] == "p300_multi_gen2"
  assert sent_values(log_entries, "dilution") == liquid_calls("dilution")  # what the limits check looks at


def sent_values(log_entries: list[dict], stage: str) -> list[tuple[str, dict]]:
  """The liquid calls a run log holds, with the values liquid_calls() lists for the stage."""
  names = {name for _, values in liquid_calls(stage) for name in values}
  return [(e["op"], {k: v for k, v in e["basic"].items() if k in names and v is not None}) for e in log_entries
          if e.get("op") in ("aspirate", "dispense", "mix") and e.get("status") == "ok"]


def tighten(folder: Path, limits: dict) -> None:
  """The person tightens the task limits by hand, as test plan E says."""
  path = folder / ".ahc" / "config.yaml"
  config = yaml.safe_load(path.read_text())
  config["limits"] = limits
  path.write_text(yaml.safe_dump(config))


@pytest.mark.parametrize("limits,refused", [
  ({"liquid_height_mm": {"min": 2}}, "aspirate liquid_height 1.5 (allowed 2..40)"),
  ({"volume_ul": {"max": 50}}, "aspirate volumes 100 (allowed 20..50)"),
  ({"flow_rate_ul_s": {"max": 50}}, "aspirate flow_rate: smoke gives none, and the task limit 1..50 leaves out the default 94"),
])
async def test_a_task_limit_the_stage_breaks_stops_it_before_the_robot_homes(tmp_path, fake_robot, limits, refused):
  assert await smoke(tmp_path, "connect", "127.0.0.1", fake_robot.port, person(), out=lambda s: None, scripted=True) == 0
  tighten(tmp_path, {"ot2.right": limits})
  ask, lines = person(), []
  code = await smoke(tmp_path, "water", None, fake_robot.port, ask, out=lines.append, scripted=True)
  text = "\n".join(lines)
  assert code == 1 and refused in text and "Nothing moved" in text, text
  assert HOMING not in ask.asked and len(ask.asked) == 1  # only the config the person confirmed, as it is
  assert [path for _, method, path, _ in fake_robot.log if method == "POST"] == []  # no run, no home, no motion


async def test_task_limits_the_stage_fits_let_it_run(tmp_path, fake_robot):
  assert await smoke(tmp_path, "connect", "127.0.0.1", fake_robot.port, person(), out=lambda s: None, scripted=True) == 0
  tighten(tmp_path, {"ot2.right": {"liquid_height_mm": {"min": 1}, "volume_ul": {"max": 100},
                                   "flow_rate_ul_s": {"min": 50, "max": 100}}})
  lines = []
  assert await smoke(tmp_path, "water", None, fake_robot.port, person(), out=lines.append, scripted=True) == 0, lines
  every_entry = [json.loads(line) for log in (tmp_path / ".ahc" / "runs").glob("*.jsonl")  # two runs: either may sort last
                 for line in log.read_text().splitlines()]
  assert sent_values(every_entry, "water") == liquid_calls("water")


def posts(bot) -> list[str]:
  return [path for _, method, path, _ in bot.log if method == "POST"]


async def test_every_stage_asks_the_person_for_the_go_ahead(tmp_path, fake_robot):
  ask = person()
  assert await smoke(tmp_path, "connect", "127.0.0.1", fake_robot.port, ask, out=lambda s: None, scripted=True) == 0
  assert len(ask.asked) == 1 and ask.asked[0].startswith("Confirm this robot")  # the config; nothing moves in connect
  for stage in ("init", "tips"):  # each stage is a new server session
    ask, lines = person(), []
    assert await smoke(tmp_path, stage, None, fake_robot.port, ask, out=lines.append, scripted=True) == 0, lines
    assert ask.asked[0].startswith(f"Go ahead with stage {stage} on this robot?") and ask.asked[1] == HOMING
    assert f"robot 'fake-ot2' at 127.0.0.1:{fake_robot.port}" in ask.asked[0]  # what the go-ahead covers
    assert "go-ahead   given by the person" in lines
    log = max((tmp_path / ".ahc" / "runs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns)  # this stage's
    ops = [json.loads(line).get("op") for line in log.read_text().splitlines()]
    assert [op for op in ops if op in ("confirm_config", "load_layout")] == ["confirm_config", "load_layout"]
  sent = len(posts(fake_robot))
  ask, lines = person(False), []  # the person does not give it: nothing moves
  assert await smoke(tmp_path, "tips", None, fake_robot.port, ask, out=lines.append, scripted=True) == 1
  assert "did not give the go-ahead" in "\n".join(lines) and HOMING not in ask.asked
  assert len(posts(fake_robot)) == sent


async def test_tips_an_earlier_stage_left_on_are_the_persons_to_clear(tmp_path, fake_robot):
  ask = person(True, True, True, False)  # config, homing, deck, then the tips are not seated straight
  assert await smoke(tmp_path, "tips", "127.0.0.1", fake_robot.port, ask, out=lambda s: None, scripted=True) == 1
  assert Workspace(tmp_path).tips_left()["mounted"] == {"ot2.right": 8}  # the stage stopped with them on
  runs = posts(fake_robot).count("/runs")
  ask, lines = person(True, True, False), []  # go-ahead, homing, then: the pipettes are not empty
  assert await smoke(tmp_path, "init", None, fake_robot.port, ask, out=lines.append, scripted=True) == 1
  text = "\n".join(lines)
  assert ask.asked[2] == "Take any tips off both pipettes by hand. Are both pipettes empty now? [yes/no] "
  assert "TIPS      Tips may still be on the pipettes: 8 on ot2.right (the server shut down" in text
  assert "Hint:" not in text and posts(fake_robot).count("/runs") == runs  # nothing started
  assert Workspace(tmp_path).tips_left() is not None
  ask, lines = person(), []  # this time they took them off
  assert await smoke(tmp_path, "init", None, fake_robot.port, ask, out=lines.append, scripted=True) == 0, lines
  assert ask.asked[1] == HOMING and ask.asked[2].startswith("Take any tips off") and ask.asked[3] == HOMING
  assert Workspace(tmp_path).tips_left() is None
  log = max((tmp_path / ".ahc" / "runs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns)
  answered = [json.loads(line) for line in log.read_text().splitlines()]
  answered = [e for e in answered if e.get("type") == "verification" and e.get("check") == "tips_mounted"]
  assert [(e["verdict"], e["judged_by"]) for e in answered] == [("pending", "human (placeholder)"), ("pass", "human")]
  assert answered[1]["note"] == "answered at the terminal (ahc-smoke)"  # the person's answer, recorded as theirs


def test_a_refusal_says_whether_the_robot_may_have_done_it():
  unknown = refusal_text(Refused("[completion_unknown] dispense on ot2.right failed after 1 command(s) went to the robot"))
  assert "may have carried out what it was sent last" in unknown and "Nothing more was sent" not in unknown
  assert "run this stage again" not in unknown
  halt = refusal_text(Refused("[device_halted] the emergency stop could not confirm that the device stopped"))
  assert "may still be finishing it" in halt
  refused = refusal_text(Refused("[device_refused] the device model refused it first"))
  assert "Nothing more was sent" in refused
  assert "Release the emergency stop first" in refusal_text(Refused("[emergency_stop] the emergency stop was pressed"))


class Hangup(Exception):
  """The terminal went away mid-stage."""


async def test_an_interrupted_stage_still_says_what_is_on_the_pipette(tmp_path, fake_robot):
  def ask(question):
    if question.startswith("8 tips on the right pipette"):
      raise Hangup()
    return True

  lines = []
  with pytest.raises((Hangup, BaseExceptionGroup)):
    await smoke(tmp_path, "tips", "127.0.0.1", fake_robot.port, ask, out=lines.append, scripted=True)
  text = "\n".join(lines)
  assert "INTERRUPTED (Hangup)" in text and "Tips are on the right pipette. Take them off by hand" in text


def test_a_closed_terminal_is_no_answer(monkeypatch):
  def closed(prompt):
    raise EOFError

  monkeypatch.setattr("builtins.input", closed)
  assert ask_terminal("Does the deck match? [yes/no] ") is False


async def test_the_robot_homes_only_after_the_person_says_so(tmp_path, fake_robot):
  ask, lines = person(True, False), []  # confirms the config, then is not ready for the robot to home
  code = await smoke(tmp_path, "tips", "127.0.0.1", fake_robot.port, ask, out=lines.append, scripted=True)
  assert code == 1 and any("not ready for the robot to home" in line for line in lines)
  assert ask.asked[1] == HOMING and "Set the deck for this stage" in "\n".join(lines)
  sent = [(method, path) for _, method, path, _ in fake_robot.log if method == "POST"]
  assert sent == []  # no run, no home: nothing reached the robot but reads
  assert not any(e.get("op") == "load_layout" for e in entries(tmp_path))


async def test_a_no_at_the_deck_check_stops_everything_before_it_moves(tmp_path, fake_robot):
  ask, lines = person(True, True, False), []  # config yes, homing yes, the deck does not match
  code = await smoke(tmp_path, "tips", "127.0.0.1", fake_robot.port, ask, out=lines.append, scripted=True)
  assert code == 1 and any("STOPPED" in line for line in lines)
  log_entries = entries(tmp_path)
  assert not any(e.get("op") == "pick_up_tips" for e in log_entries)
  assert [e["state"] for e in log_entries if e.get("type") == "gate"][-1] == "failed"
  assert not any("Tips are on" in line or "This run used" in line for line in lines)  # nothing taken from the deck


async def test_a_stop_with_tips_on_says_to_take_them_off(tmp_path, fake_robot):
  ask, lines = person(True, True, True, False), []  # ... then the tips are not seated straight
  code = await smoke(tmp_path, "tips", "127.0.0.1", fake_robot.port, ask, out=lines.append, scripted=True)
  assert code == 1, "\n".join(lines)
  text = "\n".join(lines)
  assert "Tips are on the right pipette. Take them off by hand" in text and "forgets them" in text
  assert "This run used tips from rack column 1" in text


async def test_a_refusal_after_a_dispense_names_what_the_run_used(tmp_path, fake_robot):
  fake_robot.fail_commands["dispenseInPlace"] = "pipette error"  # the water stage's dispense fails on the robot
  ask, lines = person(), []
  code = await smoke(tmp_path, "water", "127.0.0.1", fake_robot.port, ask, out=lines.append, scripted=True)
  text = "\n".join(lines)
  assert code == 1 and "REFUSED" in text, text
  assert "Tips are on the right pipette" in text
  assert "This run used tips from rack column 2 and plate column 12" in text


def test_each_stage_names_the_deck_it_needs():
  for stage in ("init", "tips", "water", "dilution"):
    text = deck_text(stage, "Check the deck")
    assert "lids off; A1 at the back left of each slot" in text and "no tips on either pipette" in text
  assert "no tips are used" in deck_text("init", "x") and "nothing is dispensed" in deck_text("tips", "x")
  assert "full tips in column 1\n" in deck_text("tips", "x")
  assert "full tips in columns 1-2" in deck_text("water", "x") and "column 12 empty" in deck_text("water", "x")
  water_then_dilution = deck_text("dilution", "x")  # column 2 went to the trash in the water stage: not needed here
  assert "full tips in columns 1 and 3-5" in water_then_dilution and "columns 1-3 empty" in water_then_dilution
  assert columns([5, 1, 3, 4]) == "columns 1 and 3-5" and columns([12]) == "column 12"


async def test_task_limits_the_person_added_stay(tmp_path, fake_robot):
  quiet = lambda line: None
  assert await smoke(tmp_path, "connect", "127.0.0.1", fake_robot.port, person(), out=quiet, scripted=True) == 0
  config_path = tmp_path / ".ahc" / "config.yaml"
  config = yaml.safe_load(config_path.read_text())
  config["limits"] = {"ot2.right": {"volume_ul": {"max": 200}, "liquid_height_mm": {"min": 1}}}  # tightened by hand
  config_path.write_text(yaml.safe_dump(config))
  for host in (None, "127.0.0.1"):  # the next stage, with or without --host: confirmed as it is, never rewritten
    ask, lines = person(), []
    assert await smoke(tmp_path, "connect", host, fake_robot.port, ask, out=lines.append, scripted=True) == 0
    if host is None:
      assert "volume_ul max 200" in ask.asked[0] and any("config     pending" in line for line in lines)
    else:
      shown = next(line for line in lines if "confirmed earlier" in line)
      assert ask.asked == [] and "volume_ul max 200" in shown and "liquid_height_mm min 1" in shown
    assert yaml.safe_load(config_path.read_text())["limits"] == config["limits"]
  from fake_robot import FakeRobot

  other = FakeRobot(name="fake-ot2-b")  # another robot: a new config, the person's limits kept
  try:
    ask = person()
    assert await smoke(tmp_path, "connect", "127.0.0.1", other.port, ask, out=quiet, scripted=True) == 0
  finally:
    other.close()
  written = yaml.safe_load(config_path.read_text())
  assert written["limits"] == config["limits"] and written["devices"][0]["options"]["port"] == other.port
  assert "volume_ul max 200" in ask.asked[0]


class _Client:
  """An MCP client stand-in: canned results per tool, and the calls made."""

  def __init__(self, results: dict):
    self.results, self.calls = results, []

  async def call_tool(self, tool: str, args: dict):
    self.calls.append((tool, args))
    return SimpleNamespace(is_error=False, structured_content=self.results.get(tool, {}), content=[])


async def test_the_person_confirms_the_summary_the_server_holds():
  config = {"status": "pending", "reason": "devices or limits changed after the last confirmation",
            "devices": [{"id": "ot2", "model": "opentrons.ot2", "backend": "robot",
                         "options": {"host": "10.0.0.5", "port": 31950, "robot_name": "lab-ot2"}}],
            "limits": {}, "confirm_summary": "ot2 = opentrons.ot2, backend robot, robot 'lab-ot2' at 10.0.0.5:31950; "
                                             "task limits: none"}
  client, ask = _Client({"lab_overview": {"config": config}}), person()
  await Smoke(client, ask, lambda line: None).configure(None, 31950)
  assert config["confirm_summary"] in ask.asked[0]
  assert client.calls[-1] == ("confirm_config", {"summary": config["confirm_summary"]})  # contract C32
  del config["confirm_summary"]  # a server that gives no summary: the same line, built here
  client, ask = _Client({"lab_overview": {"config": config}}), person()
  await Smoke(client, ask, lambda line: None).configure(None, 31950)
  assert "robot 'lab-ot2' at 10.0.0.5:31950; task limits: none" in ask.asked[0]
  assert client.calls[-1] == ("confirm_config", {})
  client, ask = _Client({"lab_overview": {"config": config}}), person(False)
  with pytest.raises(Stop):
    await Smoke(client, ask, lambda line: None).configure(None, 31950)
  assert [tool for tool, _ in client.calls] == ["lab_overview"]  # said no: nothing confirmed


async def test_an_agent_cannot_run_it(tmp_path, monkeypatch):
  for agent in ("CLAUDECODE", "AI_AGENT"):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    monkeypatch.setenv(agent, "1")
    lines = []
    assert await smoke(tmp_path, "connect", "127.0.0.1", 9, lambda q: True, out=lines.append) == 2
    assert agent in lines[0] and "The person runs ahc-smoke at a terminal" in lines[0]
  assert not (tmp_path / ".ahc").exists()
  cli = Path(sys.executable).parent / "ahc-smoke"
  env = {**{k: v for k, v in os.environ.items() if k != "CLAUDECODE"}, "AI_AGENT": "some-agent"}
  out = subprocess.run([str(cli), "--stage", "connect", "--workspace", str(tmp_path)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, env=env)
  assert out.returncode != 0 and "not from an agent" in out.stderr and "AI_AGENT" in out.stderr


def test_it_needs_the_person_at_a_terminal(tmp_path):
  cli = Path(sys.executable).parent / "ahc-smoke"
  env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
  out = subprocess.run([str(cli), "--stage", "connect", "--workspace", str(tmp_path)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, env=env)
  assert out.returncode != 0 and "terminal" in out.stderr
  assert not (tmp_path / ".ahc").exists()


async def test_an_unreachable_robot_stops_with_a_reason(tmp_path):
  lines = []
  code = await smoke(tmp_path, "connect", "127.0.0.1", 9, lambda q: True, out=lines.append, scripted=True)  # nothing listens on 9
  assert code == 1 and any("no robot-server at 127.0.0.1:9" in line for line in lines)


async def test_forced_simulation_is_refused(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_BACKEND", "sim")
  lines = []
  assert await smoke(tmp_path, "connect", "127.0.0.1", 9, lambda q: True, out=lines.append, scripted=True) == 2
  assert "AHC_BACKEND=sim" in lines[0]


async def test_its_dashboard_is_this_folders(tmp_path, fake_robot):
  other = Workspace(tmp_path / "rehearsal")
  other.root.mkdir()
  left_running = serve(other, None, 0, open_browser=False)  # an ahc-dashboard left running for another folder
  port = left_running.server_address[1]
  task = tmp_path / "task"
  task.mkdir()
  try:
    lines = []
    assert await smoke(task, "connect", "127.0.0.1", fake_robot.port, person(), out=lines.append,
                       dashboard_port=port, scripted=True) == 0
  finally:
    left_running.shutdown()
  dashboard = next(line for line in lines if line.startswith("dashboard"))
  assert f"serves the dashboard of {other.root}; its stop button does NOT stop this run" in dashboard


@needs_simulator
async def test_the_dilution_stage_on_the_simulator(tmp_path):
  ask, lines = person(), []
  code = await smoke(tmp_path, "dilution", OT2_HOST, OT2_PORT, ask, out=lines.append, scripted=True)
  assert code == 0, "\n".join(lines)
  state = replay(sorted((tmp_path / ".ahc" / "runs").glob("*.jsonl"))[-1])
  wells = state["plates"][0]["wells"]
  assert [wells[w]["v"] for w in ("A1", "A2", "A3")] == pytest.approx([100, 100, 200])
  human = [e for e in entries(tmp_path) if e.get("type") == "verification" and e.get("judged_by") == "human"]
  assert len(human) == 7 and len(ask.asked) == 9


@needs_simulator
async def test_a_second_run_keeps_the_confirmed_robot(tmp_path):
  assert await smoke(tmp_path, "connect", OT2_HOST, OT2_PORT, person(), out=lambda s: None, scripted=True) == 0
  ask = person()
  code = await smoke(tmp_path, "init", None, 31950, ask, out=lambda s: None, scripted=True)
  assert code == 0 and len(ask.asked) == 3  # no new config question: this session's go-ahead, then the home
  assert ask.asked[0].startswith("Go ahead with stage init") and ask.asked[1] == HOMING
  assert ask.asked[2].startswith("Check the deck against the layout")
