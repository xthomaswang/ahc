"""Task limits: a workspace may tighten the description file's limits, never loosen or replace them."""

import pytest
import yaml

from ahc.examples import STARLET_LAYOUT
from conftest import load_verified

pytestmark = pytest.mark.anyio

DEV = "starlet.pip"
STARLET_SIM = [{"id": "starlet", "model": "hamilton.starlet", "backend": "sim"}]


async def with_limits(lab, limits):
  await lab.call("configure_devices", devices=STARLET_SIM, limits=limits)
  await load_verified(lab, layout=STARLET_LAYOUT)
  await lab.call("pick_up_tips", device=DEV)


async def test_a_task_limit_tightens(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await with_limits(lab, {DEV: {"volume_ul": {"max": 200}}})
    refusal = await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=250)
    assert refusal == "out_of_range"
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=150)
    params = await lab.call("get_params", device=DEV, op="aspirate")
    limits = params["basic"]["volumes"]["limits"]
    assert limits["max"] == 200 and limits["task_limit"] and "1000" in limits["source"]


LOOSENING = [
  ("limit_loosened", {"volume_ul": {"max": 2000}}),            # above the description's 1000
  ("limit_loosened", {"volume_ul": {"min": 0.01}}),            # below the description's 0.1
  ("limit_loosened", {"aspirate.settling_time_s": {"max": 60}}),
  ("config_invalid", {"volume_ul": {"max": 200, "default": 100}}),  # defaults are not the task's to set
  ("config_invalid", {"volume_ul": {"type": "integer"}}),
  ("config_invalid", {"dispense.jet": {"max": 1}}),            # booleans take no limits
  ("config_invalid", {"volume_ul": {"min": 300, "max": 200}}),
  ("config_invalid", {"speed": {"max": 1}}),                   # no such limit
]


@pytest.mark.parametrize("code,limits", LOOSENING, ids=[str(l) for _, l in LOOSENING])
async def test_loosening_or_replacing_is_refused(tmp_path, open_lab, code, limits):
  async with open_lab(tmp_path, "sim") as lab:
    assert await lab.refused("configure_devices", devices=STARLET_SIM, limits={DEV: limits}) == code
  assert not (tmp_path / ".ahc" / "config.yaml").exists()  # nothing was written


async def test_a_loosening_limit_written_by_hand_blocks_motion(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    await lab.call("load_layout", layout=STARLET_LAYOUT)
    path = tmp_path / ".ahc" / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["limits"] = {DEV: {"volume_ul": {"max": 5000}}}
    path.write_text(yaml.safe_dump(config))
    overview = await lab.call("lab_overview")
    assert overview["config"]["status"] in ("pending", "invalid")
    assert await lab.refused("pick_up_tips", device=DEV) in ("config_pending", "limit_loosened")
    # Even confirming it cannot make a loosening limit valid.
    assert await lab.refused("confirm_config") == "limit_loosened"


async def test_an_omitted_tightened_parameter_needs_a_value(tmp_path, open_lab):
  # The STAR's flow rate otherwise comes from its liquid class, which may lie outside the task range.
  async with open_lab(tmp_path, "sim") as lab:
    await with_limits(lab, {DEV: {"flow_rate_ul_s": {"max": 100}}})
    assert await lab.refused("aspirate", device=DEV, targets=["diluent"], volumes=100) == "limit_requires_value"
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100, flow_rate=50)


async def test_dropping_a_task_limit_waits_for_the_person(tmp_path, open_lab):
  async with open_lab(tmp_path, "sim") as lab:
    first = await lab.call("configure_devices", devices=STARLET_SIM, limits={DEV: {"volume_ul": {"max": 150}}})
    assert first["config"]["status"] == "confirmed"
    tighter = await lab.call("configure_devices", devices=STARLET_SIM, limits={DEV: {"volume_ul": {"max": 100}}})
    assert tighter["config"]["status"] == "confirmed"  # tightening further needs nobody
    dropped = await lab.call("configure_devices", devices=STARLET_SIM)  # the agent removes the person's limit
    assert dropped["config"]["status"] == "pending" and "confirm_config" in dropped["next"]
    assert await lab.refused("load_layout", layout=STARLET_LAYOUT) == "config_pending"
    await lab.call("confirm_config")  # the person agrees
    await load_verified(lab, layout=STARLET_LAYOUT)


async def test_a_volume_cap_also_caps_the_native_mix(tmp_path, open_lab):
  # A post-dispense mix draws its volume through the tip, so the person's volume cap applies to it.
  async with open_lab(tmp_path, "sim") as lab:
    await with_limits(lab, {DEV: {"volume_ul": {"max": 150}}})
    params = await lab.call("get_params", device=DEV, op="dispense")
    assert params["specialized"]["post_mix_volume_ul"]["max"] == 150
    await lab.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
    assert await lab.refused("dispense", device=DEV, targets=["plate:A1:H1"], volumes=100,
                             specialized={"post_mix_volume_ul": 200, "post_mix_repetitions": 3}) == "out_of_range"
    await lab.call("dispense", device=DEV, targets=["plate:A1:H1"], volumes=100)  # no mix: still fine
