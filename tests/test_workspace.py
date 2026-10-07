"""Task workspaces, startup and the device config (steps 2 and 3 of the workspace design)."""

import json

import pytest
import yaml

from ahc.examples import STARLET_LAYOUT
from ahc.server import create_server
from ahc.workspace import resolve_workspace
from conftest import load_verified

pytestmark = pytest.mark.anyio

STARLET_SIM = [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]


def config_of(folder):
  return yaml.safe_load((folder / ".ahc" / "config.yaml").read_text())


def run_entries(folder):
  return [json.loads(line) for log in sorted((folder / ".ahc" / "runs").glob("*.jsonl"))
          for line in log.read_text().splitlines()]


# -- where the workspace is ---------------------------------------------------------------------

def test_workspace_resolution_order(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  monkeypatch.delenv("AHC_WORKSPACE", raising=False)
  assert resolve_workspace() == tmp_path.resolve()
  monkeypatch.setenv("AHC_WORKSPACE", str(tmp_path / "from-env"))
  assert resolve_workspace() == (tmp_path / "from-env").resolve()
  assert resolve_workspace(tmp_path / "from-arg") == (tmp_path / "from-arg").resolve()


# -- nothing is written until a tool needs to -----------------------------------------------------

async def test_starting_and_reading_leaves_the_folder_untouched(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "none" and overview["device"] == "hamilton.starlet"
    await lab.call("get_params", device="starlet.pip", op="aspirate")
    await lab.call("describe_device", component="starlet.pip")
    await lab.call("get_run")
  assert list(tmp_path.iterdir()) == []


async def test_forced_simulation_writes_its_config_on_first_use(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    config = config_of(tmp_path)
    assert config["devices"] == STARLET_SIM and config["confirmation"]["by"] == "simulation"
    assert sorted(p.name for p in (tmp_path / ".ahc").iterdir()) == ["config.yaml", "layouts", "protocols", "runs", "sim"]
    assert (await lab.call("lab_overview"))["config"]["status"] == "confirmed"
  assert run_entries(tmp_path)[0]["workspace"] == str(tmp_path.resolve())


async def test_two_workspaces_keep_separate_runs(tmp_path, open_lab):
  a, b = tmp_path / "task-a", tmp_path / "task-b"
  a.mkdir()
  b.mkdir()
  async with open_lab(a, "sim") as lab_a, open_lab(b, "sim", "hamilton.starlet") as lab_b:
    await lab_a.call("load_layout", layout=STARLET_LAYOUT)
    await load_verified(lab_b, layout=STARLET_LAYOUT)
    await lab_b.call("pick_up_tips", device="starlet.pip")
  assert len(list((a / ".ahc" / "runs").glob("*.jsonl"))) == 1
  assert [e["op"] for e in run_entries(a) if "op" in e] == ["load_layout"]
  assert [e["op"] for e in run_entries(b) if "op" in e] == ["load_layout", "pick_up_tips"]


# -- config: template, choice, confirmation -------------------------------------------------------

async def test_without_simulation_the_person_must_confirm(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "none" and "hamilton.starlet" in overview["available_models"]
    assert list(tmp_path.iterdir()) == []
    # The first write copies the template; nothing may move while it is pending.
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_pending"
    assert config_of(tmp_path)["devices"] == []
    configured = await lab.call("configure_devices", devices=STARLET_SIM)
    assert configured["config"]["status"] == "pending" and "confirm_config" in configured["next"]
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "config_pending"
    confirmed = await lab.call("confirm_config")
    assert confirmed["config"]["status"] == "confirmed" and confirmed["config"]["confirmed_by"] == "human"
    await lab.call("load_layout", layout=STARLET_LAYOUT)
  assert config_of(tmp_path)["confirmation"]["by"] == "human"


async def test_forced_simulation_confirms_a_simulation_only_choice(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    configured = await lab.call("configure_devices",
                                devices=[{"id": "robot1", "model": "hamilton.starlet", "backend": "sim"}])
    assert configured["config"]["status"] == "confirmed" and configured["config"]["confirmed_by"] == "simulation"
    overview = await lab.call("lab_overview")
    assert "robot1.pip" in overview["components"]  # addresses follow the config's device id
    await load_verified(lab, layout=STARLET_LAYOUT)
    await lab.call("pick_up_tips", device="robot1.pip")


def test_only_simulation_can_be_forced(tmp_path):
  with pytest.raises(ValueError, match="only 'sim'"):
    create_server(tmp_path, backend="usb")


async def test_a_real_backend_needs_a_description_file_backend(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    bad = [{"id": "starlet", "model": "hamilton.starlet", "backend": "usb"}]
    assert await lab.refused("configure_devices", devices=bad) == "config_invalid"
    assert await lab.refused("configure_devices", devices=STARLET_SIM * 2) == "config_invalid"


async def test_editing_a_confirmed_config_by_hand_sends_it_back_to_pending(tmp_path, open_lab):
  async with open_lab(tmp_path, backend=None) as lab:
    await lab.call("configure_devices", devices=STARLET_SIM)
    await lab.call("confirm_config")
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    config = config_of(tmp_path)
    config["limits"] = {"starlet.pip": {"volume_ul": {"max": 50}}}  # an edit the person never confirmed
    (tmp_path / ".ahc" / "config.yaml").write_text(yaml.safe_dump(config))
    assert await lab.refused("pick_up_tips", device="starlet.pip") == "config_pending"
    assert (await lab.call("lab_overview"))["config"]["reason"].startswith("devices or limits changed")
  audits = [e for e in run_entries(tmp_path) if e.get("type") == "audit"]
  assert [a["event"] for a in audits] == ["config_edited_outside_server"]


async def test_a_broken_config_keeps_the_server_up(tmp_path, open_lab):
  (tmp_path / ".ahc").mkdir()
  (tmp_path / ".ahc" / "config.yaml").write_text("devices: [\n")
  async with open_lab(tmp_path, "sim") as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "invalid" and overview["config"]["error"] == "config_invalid"
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_invalid"
    await lab.call("configure_devices", devices=STARLET_SIM)  # rewriting it is the way out
    await lab.call("load_layout", layout=STARLET_LAYOUT)


async def test_device_flag_conflicting_with_the_config_is_refused(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("load_layout", layout=STARLET_LAYOUT)
  async with open_lab(tmp_path, "sim", "opentrons.ot2") as lab:
    assert (await lab.call("lab_overview"))["config"]["error"] == "device_conflict"
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "device_conflict"
