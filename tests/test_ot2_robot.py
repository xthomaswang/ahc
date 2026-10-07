"""The real OT-2 backend ('robot'), rehearsed against an OT-2 simulator: the same robot-server HTTP API
as a real OT-2, with the person's confirmation and verdicts that real hardware needs. Needs
AHC_TEST_OT2_PORT (an ahc-sim ot2 started for testing)."""

import json
import urllib.request

import anyio
import pytest

from ahc.examples import OT2_LAYOUT
from ahc.workspace import estop
from ahc.workspace.store import Workspace
from conftest import OT2_HOST, OT2_PORT, load_verified, robot_server_up

pytestmark = pytest.mark.anyio

DEV = "ot2.right"


@pytest.fixture(autouse=True)
def needs_simulator():
  if not robot_server_up():
    pytest.skip("set AHC_TEST_OT2_PORT to a running OT-2 simulator's port (ahc-sim ot2 in a test folder)")


def robot(**options):
  return [{"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "options": options}]


async def confirmed_robot(lab, **options):
  """The config for a real OT-2 (here the simulator's address), written by the agent, confirmed by the person."""
  written = await lab.call("configure_devices", devices=robot(host=OT2_HOST, port=OT2_PORT, **options))
  assert written["config"]["status"] == "pending"  # a real backend always waits for the person
  confirmed = await lab.call("confirm_config")  # the test plays the person
  assert confirmed["config"]["status"] == "confirmed" and confirmed["config"]["confirmed_by"] == "human"


def run_status(run_id: str) -> str:
  request = urllib.request.Request(f"http://{OT2_HOST}:{OT2_PORT}/runs/{run_id}", headers={"Opentrons-Version": "*"})
  with urllib.request.urlopen(request, timeout=5) as response:
    return json.load(response)["data"]["status"]


async def test_a_robot_needs_its_address_and_the_person(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    assert await lab.refused("configure_devices", devices=robot()) == "robot_host_required"  # never localhost by default
    await confirmed_robot(lab)
    overview = await lab.call("lab_overview")
  assert overview["simulated"] is False and overview["backend"] == "robot"
  identity = overview["device_identity"]  # shown on real hardware: which robot the person confirmed
  assert identity["robot_model"] == "OT-2 Standard" and identity["pipettes"]["right"]["name"] == "p300_multi_gen2"


async def test_forced_simulation_still_wins(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:  # the plugin's server
    await lab.call("configure_devices", devices=robot(host=OT2_HOST, port=OT2_PORT))
    await lab.call("confirm_config")
    assert lab.server.lab.adapter.backend == "sim"  # a confirmed robot config still runs in simulation here


async def test_a_rehearsal_of_the_real_run(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    await confirmed_robot(lab)
    loaded = await lab.call("load_layout", layout=OT2_LAYOUT)
    assert "needs_person" not in loaded and loaded["saved_as"] == "layout-1"  # real hardware: saved at once
    check = await lab.call("verify", check="deck_matches_layout")
    assert check["verdict"] == "pending" and check["judged_by"] == "human (placeholder)"
    assert await lab.refused("pick_up_tips", device=DEV) == "gate_pending"
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    await lab.call("pick_up_tips", device=DEV)
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
    await lab.call("dispense", device=DEV, targets=["plate:A1:H1"], volumes=100)
    await lab.call("drop_tips", device=DEV)  # the dev server's version patch keeps PyLabRobot off the old trash path
    tips = await lab.call("verify", check="tips_mounted", device=DEV, expect="none")
    assert tips["verdict"] == "pending"  # every check on real hardware is the person's
    entries = lab.server.lab.runlog.entries
  device = [e for e in entries if e.get("type") == "device"][0]
  assert device["backend"] == "robot" and device["name"] and device["api_version"]
  assert device["pipettes"]["left"]["name"] == "p20_single_gen2"
  assert [e["judged_by"] for e in entries if e.get("type") == "verification" and e["verdict"] == "pass"] == ["human"]


async def test_another_robot_at_that_address_is_refused(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    await confirmed_robot(lab, robot_name="lab-ot2-7")
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[wrong_robot]" in text and "lab-ot2-7" in text


async def test_the_emergency_stop_stops_the_robots_run(tmp_path, open_lab):
  ws = Workspace(tmp_path)
  async with open_lab(tmp_path, backend=None) as lab:
    await confirmed_robot(lab)
    await load_verified(lab, layout=OT2_LAYOUT)
    run_id = lab.server.lab.adapter.device._run.id
    estop.engage(ws, by="test")
    with anyio.fail_after(10):  # the server's watcher sees the stop within 0.2 s and stops the robot's run
      while not any(e.get("event") == "device_halted" for e in lab.server.lab.runlog.entries):
        await anyio.sleep(0.1)
    assert run_status(run_id) == "stopped"
    estop.release(ws, by="person")
    await lab.call("verify", check="deck_matches_layout")  # polls the release: a new, pending gate
    check = (await lab.call("lab_overview"))["gate"]
    assert check["state"] == "pending"
    overview = await lab.call("lab_overview")
    assert "load_layout again" in overview["next"]
    await load_verified(lab, layout=OT2_LAYOUT)  # a new run: the reload works after a stop
    assert lab.server.lab.adapter.device._run.id != run_id
    await lab.call("pick_up_tips", device=DEV)
    await lab.call("drop_tips", device=DEV)
