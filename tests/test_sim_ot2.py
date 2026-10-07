"""ahc-sim ot2: one base install for all tasks, one simulator instance per task folder."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from ahc.devices.opentrons.sim import robot_server as ot2
from ahc.examples import OT2_LAYOUT
from ahc.workspace.store import Workspace
from conftest import load_verified

pytestmark = pytest.mark.anyio


@pytest.fixture
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  return tmp_path / "ahc-home"


def fake_checkout(root: Path, git: bool = False) -> Path:
  """The shape of a set-up opentrons-ot2 checkout, without the 270 MB."""
  rs = root / "robot-server"
  (rs / "robot_server").mkdir(parents=True)
  (rs / "robot_server" / "app.py").write_text("")
  (rs / ".venv" / "bin").mkdir(parents=True)
  (rs / ".venv" / "bin" / "uvicorn").write_text("")
  if git:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "fake"], check=True)
  return root


def closed_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def test_the_setup_plan_says_what_where_and_how(home):
  plan = ot2.setup_plan()
  assert ot2.COMMIT[:7] in plan and "270 MB" in plan and str(home / "sim" / "opentrons-ot2") in plan
  command = plan.rsplit("Command: ", 1)[1].split()[0]
  assert Path(command).is_file()  # absolute: under a plugin, ahc-sim is not on PATH


def test_registering_an_existing_checkout_validates_it(home, tmp_path):
  with pytest.raises(ot2.SimError, match="not a usable"):
    ot2.setup(tmp_path / "nothing-here")
  warnings = []
  base = ot2.setup(fake_checkout(tmp_path / "checkout"), out=warnings.append)
  assert base.source == "existing" and base.robot_server == (tmp_path / "checkout" / "robot-server").resolve()
  assert "validated with" in warnings[0]  # not at the pinned commit: said loudly
  assert ot2.installed_base() == base


def test_each_task_gets_its_own_instance_and_port(home, tmp_path):
  base = ot2.setup(fake_checkout(tmp_path / "checkout"), out=lambda _: None)
  a, b = Workspace(tmp_path / "task-a"), Workspace(tmp_path / "task-b")
  a.root.mkdir()
  b.root.mkdir()
  argv, env, cwd, port_a = ot2.prepare_instance(a, base)
  assert env["OT_ROBOT_SERVER_persistence_directory"] == str(a.dir / "sim" / "ot2" / "state")
  assert env["OT_API_CONFIG_DIR"] == str(a.dir / "sim" / "ot2" / "opentrons")  # not the global ~/.opentrons
  assert env["OT_ROBOT_SERVER_simulator_configuration_file_path"] == str(a.dir / "sim" / "ot2" / "sim_ot2.json")
  assert argv[-3] == str(port_a) and cwd == base.robot_server
  assert ot2.read_settings(a)["port"] == port_a
  with socket.socket() as held:  # task A's simulator holds its port
    held.bind(("127.0.0.1", port_a))
    held.listen()
    _, _, _, port_b = ot2.prepare_instance(b, base)
  assert port_b != port_a and ot2.read_settings(b)["port"] == port_b
  assert ot2.workspace_options(b) == {"host": "127.0.0.1", "port": port_b}


def test_the_same_task_twice_is_refused(home, tmp_path, monkeypatch):
  base = ot2.setup(fake_checkout(tmp_path / "checkout"), out=lambda _: None)
  ws = Workspace(tmp_path / "task")
  ws.root.mkdir()
  _, _, _, port = ot2.prepare_instance(ws, base)
  monkeypatch.setattr(ot2, "_port_free", lambda p: p != port)
  monkeypatch.setattr(ot2, "health", lambda p, *a, **k: {"name": ot2.robot_name(ws)} if p == port else None)
  with pytest.raises(ot2.SimError, match="already running"):
    ot2.prepare_instance(ws, base)
  monkeypatch.setattr(ot2, "health", lambda p, *a, **k: {"name": "another-task"})
  assert ot2.prepare_instance(ws, base)[3] != port  # another task took it: move on


async def test_the_base_version_is_recorded_without_unconfirming(home, tmp_path, open_lab):
  base = ot2.setup(fake_checkout(tmp_path / "checkout", git=True), out=lambda _: None)
  task = tmp_path / "task"
  task.mkdir()
  async with open_lab(task, "sim") as lab:
    await lab.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}])
  ot2.prepare_instance(Workspace(task), base)
  config = yaml.safe_load((task / ".ahc" / "config.yaml").read_text())
  assert base.commit and config["sim"]["opentrons-ot2"] == base.commit
  async with open_lab(task, "sim") as lab:
    assert (await lab.call("lab_overview"))["config"]["status"] == "confirmed"


async def test_a_missing_simulator_says_what_to_do(home, tmp_path, open_lab):
  port = closed_port()
  async with open_lab(tmp_path, "sim", "opentrons.ot2", {"port": port}) as lab:
    message = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[sim_not_installed]" in message and "--setup" in message and "270 MB" in message
    ot2.setup(fake_checkout(tmp_path / "checkout"), out=lambda _: None)
    message = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[sim_not_running]" in message and "ot2 --workspace" in message


CHECKOUT = os.environ.get("AHC_TEST_OT2_CHECKOUT")


@pytest.mark.skipif(not CHECKOUT, reason="set AHC_TEST_OT2_CHECKOUT to a set-up opentrons-ot2 checkout")
async def test_two_tasks_run_their_own_simulators(home, tmp_path, open_lab):
  ot2.setup(Path(CHECKOUT), out=lambda _: None)
  tasks, procs = [tmp_path / "task-a", tmp_path / "task-b"], []
  try:
    for task in tasks:
      task.mkdir()
      ahc_sim = Path(sys.executable).parent / "ahc-sim"
      procs.append(subprocess.Popen([str(ahc_sim), "ot2"], cwd=task, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env={**os.environ, "AHC_HOME": str(home)}))
      for _ in range(90):
        port = ot2.read_settings(Workspace(task)).get("port")
        if port and ot2.health(port):
          break
        time.sleep(0.5)
      else:
        raise AssertionError(f"the simulator for {task} did not answer")
    ports = [ot2.read_settings(Workspace(t))["port"] for t in tasks]
    assert ports[0] != ports[1]
    for task in tasks:
      async with open_lab(task, "sim", "opentrons.ot2") as lab:
        overview = await lab.call("lab_overview")
        assert overview["simulator"]["running"]
        await load_verified(lab, layout=OT2_LAYOUT)
        await lab.call("pick_up_tips", device="ot2.right")
        await lab.call("aspirate", device="ot2.right", targets=["diluent"], volumes=100)
        await lab.call("dispense", device="ot2.right", targets=["plate:A1:H1"], volumes=100)
        assert (await lab.call("get_run"))["commands_sent"] > 0
      config = yaml.safe_load((task / ".ahc" / "config.yaml").read_text())
      assert config["sim"]["opentrons-ot2"] == ot2.COMMIT
  finally:
    for proc in procs:
      proc.terminate()
      proc.wait(timeout=20)


async def test_a_task_never_borrows_another_tasks_simulator(home, tmp_path, open_lab):
  """No port given and no simulator started here: refuse, rather than use whatever answers on 31950."""
  async with open_lab(tmp_path, "sim", "opentrons.ot2") as lab:
    assert "[sim_not_installed]" in await lab.call_text("load_layout", layout=OT2_LAYOUT)
    ot2.setup(fake_checkout(tmp_path / "checkout"), out=lambda _: None)
    message = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[sim_not_running]" in message and "has not started its simulator" in message


async def test_a_port_held_by_another_tasks_simulator_is_refused(home, tmp_path, open_lab, monkeypatch):
  ot2.setup(fake_checkout(tmp_path / "checkout"), out=lambda _: None)
  ws = Workspace(tmp_path)
  _, _, _, port = ot2.prepare_instance(ws, ot2.installed_base())
  monkeypatch.setattr(ot2, "health", lambda p, *a, **k: {"name": "ahc-someone-else"})
  async with open_lab(tmp_path, "sim", "opentrons.ot2") as lab:
    message = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "another task's simulator" in message
