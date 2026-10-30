"""The person's go-ahead for a server session. A server that does not force simulation follows the task
folder's record: on a real robot every session needs the person's go-ahead before anything moves (load_layout
homes the robot), in simulation only a folder that has never run an experiment does, and forced simulation
never asks. The go-ahead is the person's confirm_config in this session. tests/fake_robot.py stands in for
the robot."""

import sys

import pytest

from ahc import server as server_module
from ahc.examples import OT2_LAYOUT, STARLET_LAYOUT
from ahc.workspace import Config, DeviceEntry, Workspace, simulation_config
from ahc.workspace.config import HEADER
from conftest import load_verified

pytestmark = pytest.mark.anyio

DEV = "ot2.right"
# Every tool that may move something; each is refused before the go-ahead.
MOTION = [
  ("load_layout", dict(layout=OT2_LAYOUT)),
  ("pick_up_tips", dict(device=DEV)),
  ("aspirate", dict(device=DEV, targets=["diluent"], volumes=100)),
  ("dispense", dict(device=DEV, targets=["plate:col1"], volumes=100)),
  ("mix", dict(device=DEV, targets=["plate:col1"], volume=50, repetitions=2)),
  ("drop_tips", dict(device=DEV)),
  ("invoke", dict(device=DEV, op="sense_tip_presence")),
]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


def earlier_config(folder, entry: DeviceEntry, by: str = "human") -> None:
  """A config as an earlier session left it in the folder: confirmed by the person, or by forced simulation."""
  config = Config(devices=[entry], limits={})
  config.confirm(by)
  Workspace(folder).write_config(config.data(), HEADER)


def robot_entry(bot) -> DeviceEntry:
  return DeviceEntry("ot2", "opentrons.ot2", "robot", {"host": "127.0.0.1", "port": bot.port, "robot_name": bot.name})


def posts(bot) -> list[str]:
  """What reached the robot that could move it: runs, commands, a home."""
  return [path for _, method, path, _ in bot.log if method == "POST"]


async def go_ahead(lab):
  """The person approves confirm_config with the summary the agent showed them."""
  summary = (await lab.call("lab_overview"))["config"]["confirm_summary"]
  return await lab.call("confirm_config", summary=summary)


# -- how the server is started ----------------------------------------------------------------------

def test_the_server_command_forces_simulation_only_when_asked(tmp_path, monkeypatch):
  started = []
  monkeypatch.setattr(server_module.MCPServer, "run", lambda self, *args, **kwargs: started.append(self))

  def forced(*args: str) -> bool:
    monkeypatch.setattr(sys, "argv", ["ahc-mcp", "--workspace", str(tmp_path), *args])
    server_module.main()
    return started[-1].lab.forced_sim

  assert forced("--backend", "config") is False  # the plugin with AHC_PLUGIN_BACKEND unset: the folder decides
  assert forced() is False
  assert forced("--backend", "sim") is True  # AHC_PLUGIN_BACKEND=sim still forces simulation
  monkeypatch.setenv("AHC_BACKEND", "sim")  # headless runs, evals, CI
  assert forced("--backend", "config") is True
  monkeypatch.delenv("AHC_BACKEND")
  with pytest.raises(SystemExit):  # any other AHC_PLUGIN_BACKEND: the server refuses to start
    forced("--backend", "robot")


# -- a real robot: every session ---------------------------------------------------------------------

async def test_a_robot_needs_the_persons_go_ahead_in_every_session(tmp_path, open_lab, fake_robot):
  earlier_config(tmp_path, robot_entry(fake_robot))
  homes = f"the real robot 'fake-ot2' at 127.0.0.1:{fake_robot.port} homes on load_layout"
  async with open_lab(tmp_path, backend=None) as lab:  # a new session: Claude Code started again, resumed or reconnected
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "confirmed" and overview["session"] == {"go_ahead": "needed", "why": homes}
    assert overview["next"].startswith("This session needs the person's go-ahead before anything moves")
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[session_unconfirmed]" in text and homes in text
    assert "confirm_config(summary=<that summary>)" in text and "never call it on your own" in text
    for tool, args in MOTION:
      assert await lab.refused(tool, **args) == "session_unconfirmed", tool
    assert posts(fake_robot) == []  # no run created, no home
    confirmed = await go_ahead(lab)
    assert confirmed["config"]["confirmed_by"] == "human"
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "given"
    await load_verified(lab, layout=OT2_LAYOUT)
    assert {"/runs", "/robot/home"} <= set(posts(fake_robot))  # now the run starts and the robot homes
  sent = len(posts(fake_robot))
  async with open_lab(tmp_path, backend=None) as lab:  # the next session asks again
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "needed"
    assert await lab.refused("load_layout", layout=OT2_LAYOUT) == "session_unconfirmed"
  assert len(posts(fake_robot)) == sent


async def test_reading_checking_and_reporting_need_no_go_ahead(tmp_path, open_lab, fake_robot):
  earlier_config(tmp_path, robot_entry(fake_robot))
  async with open_lab(tmp_path, backend=None) as lab:
    for tool in ("lab_overview", "get_state", "find_layout_references", "list_checks", "get_run"):
      await lab.call(tool)
    await lab.call("describe_device", component=DEV)
    await lab.call("get_params", device=DEV, op="aspirate")
    await lab.call("report_decision", decision="Ask the person for the go-ahead", waiting_for="person")
    check = await lab.call("verify", check="tips_mounted", device=DEV, expect="none")
    assert check["verdict"] == "pending"
    await lab.call("record_verdict", check_id=check["check_id"], verdict="pass")
    written = await lab.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "robot",
                                                            "options": robot_entry(fake_robot).options}])
    assert written["config"]["status"] == "confirmed"  # the same config: its confirmation stays, the session still waits
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "needed"
  assert posts(fake_robot) == []


async def test_a_config_another_session_confirmed_needs_this_sessions_go_ahead(tmp_path, open_lab, fake_robot):
  starlet = {"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}
  async with open_lab(tmp_path, backend=None) as here, open_lab(tmp_path, backend=None) as elsewhere:
    await here.call("configure_devices", devices=[starlet])
    await go_ahead(here)  # the person's go-ahead for the simulation, in this session
    assert (await here.call("lab_overview"))["session"]["go_ahead"] == "given"
    robot = {"id": "ot2", "model": "opentrons.ot2", "backend": "robot", "options": {"host": "127.0.0.1",
                                                                                    "port": fake_robot.port}}
    await elsewhere.call("configure_devices", devices=[robot])  # another session in the same folder
    await go_ahead(elsewhere)
    session = (await here.call("lab_overview"))["session"]  # this server now binds the robot
    assert session["go_ahead"] == "needed" and "the real robot 'fake-ot2'" in session["why"]
    assert await here.refused("load_layout", layout=OT2_LAYOUT) == "session_unconfirmed"
  assert posts(fake_robot) == []


# -- simulation: only a folder that never ran an experiment ------------------------------------------

async def test_a_simulation_needs_it_until_the_folder_has_run_an_experiment(tmp_path, open_lab):
  earlier_config(tmp_path, DeviceEntry("starlet", "hamilton.starlet", "sim"))  # the person confirmed it earlier
  async with open_lab(tmp_path, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["session"] == {"go_ahead": "needed", "why": "this folder has never run an experiment"}
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)
    assert "[session_unconfirmed]" in text and "this folder has never run an experiment" in text
    assert not Workspace(tmp_path).ran_experiment()
    await go_ahead(lab)
    await load_verified(lab, layout=STARLET_LAYOUT)
  assert Workspace(tmp_path).ran_experiment()
  async with open_lab(tmp_path, backend=None) as lab:  # a later session: the folder has run one
    overview = await lab.call("lab_overview")
    assert overview["session"]["go_ahead"] == "not needed" and not overview["next"].startswith("This session")
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="starlet.pip")


async def test_an_automatic_simulation_config_needs_it_in_a_folder_without_a_run(tmp_path, open_lab):
  Workspace(tmp_path).write_config(simulation_config("hamilton.starlet").data(), HEADER)  # left by forced simulation
  async with open_lab(tmp_path, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["confirmed_by"] == "simulation" and overview["config"]["status"] == "confirmed"
    assert overview["session"]["go_ahead"] == "needed"
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "session_unconfirmed"
    confirmed = await go_ahead(lab)
    assert confirmed["config"]["confirmed_by"] == "human"  # stamped again, by the person
    await lab.call("load_layout", layout=STARLET_LAYOUT)


async def test_a_new_config_confirmed_in_this_session_is_the_go_ahead(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "none" and overview["session"]["go_ahead"] == "needed"
    assert "whether to simulate or use a real robot" in overview["next"]
    text = await lab.call_text("describe_device", component="starlet.pip")  # no config yet: the refusals ask as well
    assert "[no_config]" in text and "whether to simulate or use a real robot" in text.partition("Hint:")[2]
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)  # a template, no device chosen yet
    assert "[config_pending]" in text and "whether to simulate or use a real robot" in text.partition("Hint:")[2]
    await lab.call("configure_devices", devices=[{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}])
    text = await lab.call_text("load_layout", layout=STARLET_LAYOUT)  # the config comes first
    assert "[config_pending]" in text and "Show the person config.confirm_summary" in text.partition("Hint:")[2]
    await go_ahead(lab)
    assert (await lab.call("lab_overview"))["session"]["go_ahead"] == "given"
    await lab.call("load_layout", layout=STARLET_LAYOUT)


async def test_forced_simulation_never_asks(folder_pair, open_lab, fake_robot):
  fresh, robot_folder = folder_pair
  async with open_lab(fresh, "sim") as lab:  # a folder that never ran an experiment
    assert (await lab.call("lab_overview"))["session"] == {"go_ahead": "not needed",
                                                           "why": "this server forces simulation"}
    await lab.call("load_layout", layout=STARLET_LAYOUT)
  earlier_config(robot_folder, robot_entry(fake_robot))
  async with open_lab(robot_folder, "sim", "opentrons.ot2") as lab:  # the robot's folder, simulated
    overview = await lab.call("lab_overview")
    assert overview["session"]["go_ahead"] == "not needed" and overview["next"].startswith("Simulation only")
    note = overview["config"]["note"]  # and how a server that drives the robot is started instead
    assert "without AHC_BACKEND=sim" in note and "follows this folder's config" in note and "PLUGIN" not in note
  assert fake_robot.log == []


@pytest.fixture
def folder_pair(tmp_path):
  pair = tmp_path / "fresh", tmp_path / "robot"
  for folder in pair:
    folder.mkdir()
  return pair
