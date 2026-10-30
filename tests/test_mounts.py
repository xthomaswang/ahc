"""The OT-2's components are the pipettes its mounts carry: read from the robot, confirmed with it, and checked
against the confirmed config rather than the description file. Against tests/fake_robot.py; nothing here
leaves this machine."""

import pytest

from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.devices.spec import device_spec
from ahc.examples import OT2_LAYOUT
from ahc.workspace import Config, DeviceEntry, Workspace
from ahc.workspace.config import HEADER
from conftest import load_verified, robot_entry, robot_lab

pytestmark = pytest.mark.anyio

P1000 = {"name": "p1000_single_gen2", "model": "p1000_single_v2.2", "id": "R1"}
P300 = {"name": "p300_single_gen2", "model": "p300_single_v2.1", "id": "L1"}
SINGLES = {"left": "p300_single_gen2", "right": "p1000_single_gen2"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("AHC_HOME", str(tmp_path / "ahc-home"))
  monkeypatch.delenv("AHC_BACKEND", raising=False)


@pytest.fixture
def singles(fake_robot):
  """A robot as a lab has it: a P1000 single-channel on the right, a P300 single-channel on the left."""
  fake_robot.pipettes["right"], fake_robot.pipettes["left"] = dict(P1000), dict(P300)
  return fake_robot


def posts(bot):
  return [path for _, method, path, _ in bot.log if method == "POST"]


async def test_configure_reads_the_mounts_and_the_components_follow(tmp_path, open_lab, singles):
  async with open_lab(tmp_path, backend=None) as lab:
    written = await lab.call("configure_devices", devices=robot_entry("127.0.0.1", singles.port))
    config = written["config"]
    assert config["devices"][0]["options"]["mounts"] == SINGLES
    assert config["devices"][0]["options"]["robot_name"] == "fake-ot2"
    assert config["confirm_summary"] == (f"ot2 = opentrons.ot2, backend robot, robot 'fake-ot2' at 127.0.0.1:{singles.port}"
                                         ", mounts left p300_single_gen2, right p1000_single_gen2; task limits: none")
    confirmed = await lab.call("confirm_config", summary=config["confirm_summary"])
    assert confirmed["config"]["status"] == "confirmed"
    overview = await lab.call("lab_overview")
    assert list(overview["components"]) == ["ot2.left", "ot2.right"]
    assert overview["mounts"] == {"mounted": SINGLES}
    left = await lab.call("describe_device", component="ot2.left")
    right = await lab.call("describe_device", component="ot2.right")
    assert (left["channels"], left["shared_container"], left["instrument"]) == (1, True, "p300_single_gen2")
    assert left["limits"]["volume_ul"]["min"] == 20 and left["limits"]["volume_ul"]["max"] == 300
    assert left["limits"]["flow_rate_ul_s"]["max"] == 275 and left["limits"]["flow_rate_ul_s"]["default"] == 92.86
    assert (right["channels"], right["instrument"]) == (1, "p1000_single_gen2")
    assert right["limits"]["volume_ul"]["min"] == 100 and right["limits"]["volume_ul"]["max"] == 1000
    assert right["limits"]["flow_rate_ul_s"]["default"] == 274.7
    assert right["title"] == "P1000 single-channel GEN2 on the right mount"
    assert "mounts" in (await lab.call("describe_device"))
  assert posts(singles) == []  # configuring and confirming moved nothing


async def test_the_model_description_lists_the_instruments_it_describes(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    described = await lab.call("describe_device", component="opentrons.ot2")
  assert set(described["mounts"]["instruments"]) == {"p20_single_gen2", "p20_multi_gen2", "p300_single_gen2",
                                                     "p300_multi_gen2", "p1000_single_gen2"}
  assert "P300 8-channel right" not in described["guide"]


async def test_stated_mounts_that_match_the_robot_are_kept(tmp_path, open_lab, singles):
  async with open_lab(tmp_path, backend=None) as lab:
    written = await lab.call("configure_devices", devices=robot_entry("127.0.0.1", singles.port, mounts=SINGLES))
  assert written["config"]["devices"][0]["options"]["mounts"] == SINGLES


async def test_stated_mounts_that_differ_from_the_robot_write_nothing(tmp_path, open_lab, singles):
  async with open_lab(tmp_path, backend=None) as lab:
    text = await lab.call_text("configure_devices", devices=robot_entry(
      "127.0.0.1", singles.port, mounts={"left": "p300_single_gen2", "right": "p300_multi_gen2"}))
  assert "[mount_mismatch]" in text and "nothing was written" in text
  assert "right 'p1000_single_gen2'" in text and "right 'p300_multi_gen2'" in text
  assert "Leave mounts out" in text and "tell the person" in text
  assert not (tmp_path / ".ahc" / "config.yaml").exists() and posts(singles) == []


async def test_an_empty_mount_has_no_component(tmp_path, open_lab, singles):
  singles.pipettes["left"] = None
  async with robot_lab(open_lab, tmp_path, singles) as lab:
    overview = await lab.call("lab_overview")
    assert list(overview["components"]) == ["ot2.right"] and overview["mounts"]["mounted"]["left"] is None
    assert "mounts left empty, right p1000_single_gen2" in overview["config"]["confirm_summary"]
    text = await lab.call_text("describe_device", component="ot2.left")
    assert "[unknown_component]" in text and "Use one of: ot2.right" in text
    assert await lab.refused("pick_up_tips", device="ot2.left") == "unknown_component"
  assert posts(singles) == []


async def test_a_pipette_the_file_does_not_describe_is_not_driven(tmp_path, open_lab, singles):
  singles.pipettes["left"] = {"name": "p50_single_gen2", "model": "p50_single_v2.2", "id": "L9"}
  async with robot_lab(open_lab, tmp_path, singles) as lab:
    overview = await lab.call("lab_overview")
    assert list(overview["components"]) == ["ot2.right"]
    assert overview["mounts"]["not_driven"] == {"left": "p50_single_gen2"}
    assert "left p50_single_gen2 (not driven), right p1000_single_gen2" in overview["config"]["confirm_summary"]
    assert "[unknown_component]" in await lab.call_text("describe_device", component="ot2.left")


@pytest.mark.parametrize("carried", [(None, None), ("p50_single_gen2", None)])
async def test_a_robot_with_no_pipette_ahc_drives_is_refused(tmp_path, open_lab, fake_robot, carried):
  for mount, name in zip(("left", "right"), carried):
    fake_robot.pipettes[mount] = {"name": name, "model": "m", "id": "x"} if name else None
  async with open_lab(tmp_path, backend=None) as lab:
    text = await lab.call_text("configure_devices", devices=robot_entry("127.0.0.1", fake_robot.port))
  assert "[no_usable_pipette]" in text and "nothing was written" in text
  assert "p300_single_gen2" in text and "p1000_single_gen2" in text  # what AHC drives
  assert "left empty" in text or "p50_single_gen2" in text
  assert not (tmp_path / ".ahc" / "config.yaml").exists()


async def test_task_limits_on_a_missing_component_are_refused(tmp_path, open_lab, singles):
  singles.pipettes["left"] = None
  limits = {"ot2.left": {"volume_ul": {"max": 200}}}
  async with open_lab(tmp_path, backend=None) as lab:
    text = await lab.call_text("configure_devices", devices=robot_entry("127.0.0.1", singles.port), limits=limits)
    assert "[config_invalid]" in text and "ot2.left" in text and "Components: ot2.right" in text
    assert not (tmp_path / ".ahc" / "config.yaml").exists()
    ok = await lab.call("configure_devices", devices=robot_entry("127.0.0.1", singles.port),
                        limits={"ot2.right": {"volume_ul": {"max": 500}}})
    assert ok["config"]["limits"] == {"ot2.right": {"volume_ul": {"max": 500}}}


async def test_task_limits_are_checked_against_the_mounts_in_simulation_too(tmp_path, open_lab):
  sim = [{"id": "ot2", "model": "opentrons.ot2", "backend": "sim",
          "options": {"mounts": {"left": "p300_single_gen2", "right": None}}}]
  async with open_lab(tmp_path, backend=None) as lab:
    text = await lab.call_text("configure_devices", devices=sim, limits={"ot2.right": {"volume_ul": {"max": 500}}})
    assert "[config_invalid]" in text and "Components: ot2.left" in text
    assert not (tmp_path / ".ahc" / "config.yaml").exists()
    # the default pair has both mounts: the same limit is fine without the mounts option
    default = await lab.call("configure_devices", devices=[{"id": "ot2", "model": "opentrons.ot2", "backend": "sim"}],
                             limits={"ot2.right": {"volume_ul": {"max": 250}}})
    assert default["config"]["limits"]


async def test_changed_pipettes_refuse_the_layout_and_nothing_moves(tmp_path, open_lab, singles):
  async with robot_lab(open_lab, tmp_path, singles) as lab:
    singles.pipettes["right"] = {"name": "p300_single_gen2", "model": "p300_single_v2.1", "id": "R2"}
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
  assert "[mount_mismatch]" in text and "nothing was moved" in text
  assert "right mount has 'p300_single_gen2', the confirmed config names 'p1000_single_gen2'" in text
  assert "configure_devices" in text and "person confirms" in text
  assert posts(singles) == []  # no run created, no home


async def test_pipettes_changed_after_the_check_stop_the_run_again(tmp_path, open_lab, singles, monkeypatch):
  async with robot_lab(open_lab, tmp_path, singles) as lab:
    singles.pipettes["left"] = None
    monkeypatch.setattr(OT2Adapter, "identity", lambda self: {"mounts": {"ok": True, "problems": []}})  # the check missed it
    text = await lab.call_text("load_layout", layout=OT2_LAYOUT)
    assert "[mount_mismatch]" in text and "left mount is empty, the confirmed config names 'p300_single_gen2'" in text
    assert lab.server.lab.adapter.device is None  # the run it set up was stopped


async def test_an_old_config_without_mounts_means_the_default_pair(tmp_path, open_lab, fake_robot):
  config = Config(devices=[DeviceEntry("ot2", "opentrons.ot2", "robot", {"host": "127.0.0.1", "port": fake_robot.port,
                                                                         "robot_name": fake_robot.name})], limits={})
  config.confirm("human")
  Workspace(tmp_path).write_config(config.data(), HEADER)
  async with open_lab(tmp_path, backend=None) as lab:
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] == "confirmed" and "mounts left p20_single_gen2, right p300_multi_gen2" in overview["config"]["confirm_summary"]
    right = await lab.call("describe_device", component="ot2.right")
    assert right["channels"] == 8 and right["instrument"] == "p300_multi_gen2"
  assert device_spec("opentrons.ot2").mounted == {"left": "p20_single_gen2", "right": "p300_multi_gen2"}


async def test_the_default_pair_loads_a_layout_on_a_default_robot(tmp_path, open_lab, fake_robot):
  async with robot_lab(open_lab, tmp_path, fake_robot) as lab:
    await load_verified(lab, layout=OT2_LAYOUT)
