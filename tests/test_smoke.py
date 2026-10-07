"""ahc-smoke rehearsed against an OT-2 simulator as the robot: a whole stage with a person who answers
yes, a person who says no, a second run in the same folder, and the terminal it insists on."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ahc.smoke import plan_for, smoke
from ahc.viz.dashboard import replay
from conftest import OT2_HOST, OT2_PORT, robot_server_up

pytestmark = pytest.mark.anyio
needs_simulator = pytest.mark.skipif(not robot_server_up(), reason="set AHC_TEST_OT2_PORT to a running OT-2 simulator")


def entries(folder: Path) -> list[dict]:
  log = sorted((folder / ".ahc" / "runs").glob("*.jsonl"))[-1]
  return [json.loads(line) for line in log.read_text().splitlines()]


@needs_simulator
async def test_the_dilution_stage_end_to_end(tmp_path):
  asked, lines = [], []
  code = await smoke(tmp_path, "dilution", OT2_HOST, OT2_PORT, lambda q: asked.append(q) or True, out=lines.append)
  assert code == 0, "\n".join(lines)
  log = sorted((tmp_path / ".ahc" / "runs").glob("*.jsonl"))[-1]
  state = replay(log)
  agent = state["agent"]
  assert agent["plan"] == plan_for("dilution") and set(agent["steps"]) == {"done"} and agent["plan_done"]
  wells = state["plates"][0]["wells"]  # 100, 100, 200 uL; the dye at 1, 1/2, 1/4
  assert [wells[w]["v"] for w in ("A1", "A2", "A3")] == pytest.approx([100, 100, 200])
  assert [wells[w]["c"] for w in ("A1", "A2", "A3")] == pytest.approx([1, 0.5, 0.25])
  log_entries = entries(tmp_path)
  human = [e for e in log_entries if e.get("type") == "verification" and e.get("judged_by") == "human"]
  assert len(human) == 7 and all(e["verdict"] == "pass" for e in human)  # deck, 2 tip checks, 3 more, the plate
  assert len(asked) == 8 and asked[0].startswith("Confirm this robot")
  device = [e for e in log_entries if e.get("type") == "device"][0]
  assert device["backend"] == "robot" and device["pipettes"]["right"]["name"] == "p300_multi_gen2"


@needs_simulator
async def test_a_no_stops_everything_before_it_moves(tmp_path):
  answers = iter([True, False])  # confirms the config, then says the deck does not match
  lines = []
  code = await smoke(tmp_path, "tips", OT2_HOST, OT2_PORT, lambda q: next(answers), out=lines.append)
  assert code == 1 and any("STOPPED" in line for line in lines)
  log_entries = entries(tmp_path)
  assert not any(e.get("op") == "pick_up_tips" for e in log_entries)
  assert [e["state"] for e in log_entries if e.get("type") == "gate"][-1] == "failed"


@needs_simulator
async def test_a_second_run_keeps_the_confirmed_robot(tmp_path):
  assert await smoke(tmp_path, "connect", OT2_HOST, OT2_PORT, lambda q: True, out=lambda s: None) == 0
  asked = []
  code = await smoke(tmp_path, "init", None, 31950, lambda q: asked.append(q) or True, out=lambda s: None)
  assert code == 0 and len(asked) == 1 and asked[0].startswith("Check the deck")  # no new config question


def test_it_needs_the_person_at_a_terminal(tmp_path):
  cli = Path(sys.executable).parent / "ahc-smoke"
  out = subprocess.run([str(cli), "--stage", "connect", "--workspace", str(tmp_path)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
  assert out.returncode != 0 and "terminal" in out.stderr
  assert not (tmp_path / ".ahc").exists()


async def test_an_unreachable_robot_stops_with_a_reason(tmp_path):
  lines = []
  code = await smoke(tmp_path, "connect", "127.0.0.1", 9, lambda q: True, out=lines.append)  # nothing listens on 9
  assert code == 1 and any("no robot-server at 127.0.0.1:9" in line for line in lines)


async def test_forced_simulation_is_refused(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_BACKEND", "sim")
  lines = []
  assert await smoke(tmp_path, "connect", "127.0.0.1", 9, lambda q: True, out=lines.append) == 2
  assert "AHC_BACKEND=sim" in lines[0]
