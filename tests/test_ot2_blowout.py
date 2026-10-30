"""Blow-out, touch tip and air gap on the OT-2: the specialized parameters, the robot-server commands behind them
and the twin's volumes. The first tests need no simulator; the rest run against AHC_TEST_OT2_PORT's simulator
(ahc-sim ot2 started for testing, with its default mounts: a P20 single on the left, a P300 8-channel on the right)."""

import json
import urllib.request

import pytest
from pylabrobot.io.http import HTTPError
from pylabrobot.opentrons.operations import OperationLock
from pylabrobot.resources import Coordinate

from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.devices.spec import device_spec, load_device
from ahc.examples import OT2_LAYOUT, plate_volumes
from conftest import OT2_HOST, OT2_PORT, load_verified

pytestmark = pytest.mark.anyio

DEV = "ot2.right"  # the P300 8-channel, with 300 uL tips
ALL = {"blow_out", "blow_out_flow_rate_ul_s", "blow_out_height_mm", "touch_tip", "touch_tip_v_offset_mm",
       "touch_tip_radius"}


# -- no simulator ---------------------------------------------------------------------------------

def test_every_instrument_declares_the_new_parameters_with_bounds():
  spec = load_device("opentrons.ot2")
  for name in spec.instruments:
    comp = device_spec("opentrons.ot2", {"mounts": {"left": name}}).components["left"]
    dispense, aspirate, mix = (comp.specialized[op] for op in ("dispense", "aspirate", "mix"))
    assert set(dispense) == ALL and set(aspirate) == {"air_gap_ul"} and set(mix) == {"blow_out"}
    flow = dispense["blow_out_flow_rate_ul_s"]  # the pipette's own limits and default: one value per instrument
    assert flow.min < flow.default < flow.max and flow.doc and flow.source and flow.type == "number"
    assert (aspirate["air_gap_ul"].min, aspirate["air_gap_ul"].default) == (0, 0)
    assert aspirate["air_gap_ul"].max == comp.limits["volume_ul"].max
    assert (dispense["blow_out_height_mm"].min, dispense["blow_out_height_mm"].max) == (-5, 5)
    assert dispense["blow_out_height_mm"].default == 0 and dispense["touch_tip_v_offset_mm"].default == -1
    assert (dispense["touch_tip_radius"].min, dispense["touch_tip_radius"].max) == (0.5, 1.0)
    assert dispense["blow_out"].type == dispense["touch_tip"].type == mix["blow_out"].type == "boolean"


def test_blow_out_rates_are_the_pipettes_own():
  expected = {"p20_single_gen2": (0.08, 24, 7.56), "p20_multi_gen2": (0.08, 24, 7.6),
              "p300_single_gen2": (1, 275, 92.86), "p300_multi_gen2": (1, 275, 94),
              "p1000_single_gen2": (3, 812, 274.7)}
  for name, (low, high, default) in expected.items():
    flow = device_spec("opentrons.ot2", {"mounts": {"left": name}}).components["left"].specialized["dispense"][
      "blow_out_flow_rate_ul_s"]
    assert (flow.min, flow.max, flow.default) == (low, high, default), name


def adapter() -> OT2Adapter:
  built = OT2Adapter(device_spec("opentrons.ot2"), "sim", {})
  built._reset_tips()
  return built


def test_a_dispense_pushes_the_air_in_front_of_its_liquid_and_only_that():
  ot2, comp = adapter(), device_spec("opentrons.ot2").components["right"]
  ot2._push(comp, "liquid", 100)  # A, then an air gap under it, then B on the end: [B, air, A]
  ot2._push(comp, "air", 20)
  ot2._push(comp, "liquid", 50)
  assert ot2._air_held(comp) == 20
  assert ot2._air_ahead(comp, 50) == 0  # B first: the air is behind it
  ot2._deliver(comp, 50)
  assert ot2._air_ahead(comp, 30) == 20 and ot2._air_ahead(comp, 100) == 20
  ot2._deliver(comp, 30)  # the air went with the first of A
  assert ot2._air_held(comp) == 0 and ot2._tip_air[comp.name] == [["liquid", 70]]
  ot2._deliver(comp, 70)
  assert ot2._tip_air[comp.name] == []


def test_transport_air_counts_what_the_tip_holds_and_draws_for_an_aspirate_only():
  ot2, comp = adapter(), device_spec("opentrons.ot2").components["right"]
  ot2._push(comp, "air", 15)
  assert ot2.transport_air("aspirate", comp, [None] * 8, {"air_gap_ul": 10}) == [25.0] * 8
  assert ot2.transport_air("aspirate", comp, [None], {}) == [15.0]
  assert ot2.transport_air("dispense", comp, [None], {"air_gap_ul": 10}) == [0.0]
  ot2._reset_tips(comp.name)
  assert ot2.transport_air("aspirate", comp, [None], {}) == [0.0]


class StubRun:
  def __init__(self, unknown=()):
    self.sent, self.unknown = [], set(unknown)

  async def execute(self, command, params):
    if command in self.unknown:
      raise HTTPError("POST", "http://127.0.0.1/runs/x/commands", 422, "unknown commandType")
    self.sent.append((command, params))


class StubPipette:
  pipette_id = "pip-1"
  _spec = type("Spec", (), {"default_aspiration_flow_rate": 94.0})()

  def _require_active(self):
    pass

  def _liquid_targets(self, containers, offset, height):
    return (), Coordinate(1, 2, height)

  async def _move_to(self, *args, **kwargs):
    pass

  async def _retract_to_traversal_height(self):
    pass


@pytest.mark.parametrize("unknown, command", [((), "airGapInPlace"), (("airGapInPlace",), "aspirateInPlace")])
async def test_the_air_gap_falls_back_to_a_plain_aspirate_on_a_robot_server_without_the_command(unknown, command):
  ot2, comp = adapter(), device_spec("opentrons.ot2").components["right"]
  run = StubRun(unknown)
  ot2.device = type("Device", (), {"_operation_lock": OperationLock(), "_run": run, "traversal_height": 120,
                                   "right_pipette": StubPipette()})()
  ot2._well = lambda container: (10.0, Coordinate(6, 6, 0))
  note = await ot2._air_gap(comp, [object()], 20.0, None)
  assert run.sent == [(command, {"pipetteId": "pip-1", "volume": 20.0, "flowRate": 94.0})]
  assert command in note and ot2._air_held(comp) == 20


# -- against the simulator ------------------------------------------------------------------------

def robot_commands(lab) -> list[dict]:
  """The commands the simulator's robot-server holds for this run, oldest first."""
  run_id = lab.server.lab.adapter.run_id()
  request = urllib.request.Request(f"http://{OT2_HOST}:{OT2_PORT}/runs/{run_id}/commands?pageLength=1000",
                                   headers={"Opentrons-Version": "*"})
  with urllib.request.urlopen(request, timeout=10) as response:
    return json.load(response)["data"]


def since(lab, mark: int) -> list[dict]:
  return robot_commands(lab)[mark:]


def kinds(commands) -> list[str]:
  return [c["commandType"] for c in commands]


async def ready(lab):
  await load_verified(lab, layout=OT2_LAYOUT)
  await lab.call("pick_up_tips", device=DEV)
  return len(robot_commands(lab))


def count(lab) -> int:
  return lab.server.lab.adapter.command_count()


async def test_get_params_shows_the_new_parameters_per_op(ot2):
  params = await ot2.call("get_params", device=DEV, op="dispense")
  assert set(params["specialized"]) == ALL
  assert params["specialized"]["blow_out_flow_rate_ul_s"]["default"] == 94
  left = await ot2.call("get_params", device="ot2.left", op="dispense")
  assert left["specialized"]["blow_out_flow_rate_ul_s"]["max"] == 24
  assert left["specialized"]["blow_out_flow_rate_ul_s"]["default"] == 7.56
  assert set((await ot2.call("get_params", device=DEV, op="aspirate"))["specialized"]) == {"air_gap_ul"}
  assert set((await ot2.call("get_params", device=DEV, op="mix"))["specialized"]) == {"blow_out"}


async def test_a_dispense_blows_out_and_touches_the_tip(ot2):
  mark = await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  mark = len(robot_commands(ot2))
  out = await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100,
                       specialized={"blow_out": True, "touch_tip": True})
  assert "blew out at 94 uL/s, 0 mm from the well top" in out["note"] and "four sides of the well 1 mm" in out["note"]
  commands = since(ot2, mark)
  assert kinds(commands).count("dispenseInPlace") == 1 and kinds(commands).count("blowOutInPlace") == 1
  assert out["commands_sent"] == len(commands)
  blow_at = kinds(commands).index("blowOutInPlace")
  assert kinds(commands)[blow_at - 1] == "moveToCoordinates"
  top = commands[blow_at - 1]["params"]["coordinates"]
  touches = [c["params"] for c in commands[blow_at + 1:] if c["commandType"] == "moveToCoordinates" and c["params"].get("speed")]
  assert len(touches) == 6 and {t["speed"] for t in touches} == {60.0}  # right, left, centre, forward, back, centre
  assert all(abs(t["coordinates"]["z"] - (top["z"] - 1)) < 1e-6 for t in touches)
  xs = sorted({round(t["coordinates"]["x"] - top["x"], 2) for t in touches})
  assert xs == [-3.43, 0, 3.43]  # the well radius of the Corning 96 flat plate
  assert all(v == pytest.approx(100.0) for v in plate_volumes(await ot2.call("get_state", device=DEV)).values())
  assert (await ot2.call("get_state", device=DEV))["liquids_ul"]["reagents"]["A1"] == pytest.approx(1700.0)


async def test_blow_out_height_flow_rate_and_touch_geometry_follow_the_parameters(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  mark = len(robot_commands(ot2))
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100, specialized={"blow_out": True})
  plain = since(ot2, mark)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)  # another aspirate in the same tip
  mark = len(robot_commands(ot2))
  out = await ot2.call("dispense", device=DEV, targets=["plate:col2"], volumes=100,
                       specialized={"blow_out": True, "blow_out_height_mm": 3, "blow_out_flow_rate_ul_s": 50,
                                    "touch_tip": True, "touch_tip_v_offset_mm": -2.5, "touch_tip_radius": 0.5})
  commands = since(ot2, mark)
  blows = [i for i, c in enumerate(commands) if c["commandType"] == "blowOutInPlace"]
  assert commands[blows[0]]["params"]["flowRate"] == 50 and "50 uL/s, 3 mm" in out["note"]
  top = plain[kinds(plain).index("blowOutInPlace") - 1]["params"]["coordinates"]  # the well top: height 0
  at = commands[blows[0] - 1]["params"]["coordinates"]
  assert at["z"] == pytest.approx(top["z"] + 3)
  touches = [c["params"]["coordinates"] for c in commands[blows[0] + 1:] if c["params"].get("speed")]
  assert {round(t["z"], 2) for t in touches} == {round(top["z"] - 2.5, 2)}
  assert max(abs(t["x"] - at["x"]) for t in touches) == pytest.approx(3.43 / 2)  # half the well radius
  assert "prepareToAspirate" not in kinds(plain)


async def test_an_aspirate_after_a_blow_out_prepares_the_plunger_above_the_well_first(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100, specialized={"blow_out": True})
  mark = len(robot_commands(ot2))
  out = await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  commands = since(ot2, mark)
  assert "prepareToAspirate" in out["note"]
  assert kinds(commands).index("prepareToAspirate") < kinds(commands).index("aspirateInPlace")
  assert all(c["status"] == "succeeded" for c in commands)
  prepared = commands[kinds(commands).index("prepareToAspirate") - 1]["params"]["coordinates"]
  drawn = commands[kinds(commands).index("aspirateInPlace") - 1]["params"]["coordinates"]
  assert prepared["z"] > drawn["z"] + 20  # above the liquid, at the well top
  mark = len(robot_commands(ot2))  # once prepared, no more
  await ot2.call("dispense", device=DEV, targets=["plate:col2"], volumes=100)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  assert "prepareToAspirate" not in kinds(since(ot2, mark))


async def test_a_mix_blows_out_and_a_mix_after_it_works(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)  # the tip is empty again
  mark = len(robot_commands(ot2))
  out = await ot2.call("mix", device=DEV, targets=["plate:col1"], volume=50, repetitions=3, specialized={"blow_out": True})
  commands = since(ot2, mark)
  assert "blew out at 94 uL/s" in out["note"] and kinds(commands).count("blowOutInPlace") == 1
  assert kinds(commands).index("blowOutInPlace") > max(i for i, k in enumerate(kinds(commands)) if k == "dispenseInPlace")
  mark = len(robot_commands(ot2))
  out = await ot2.call("mix", device=DEV, targets=["plate:col1"], volume=50, repetitions=2)  # blow-out, then mix
  assert "prepareToAspirate" in out["note"] and all(c["status"] == "succeeded" for c in since(ot2, mark))
  await ot2.call("mix", device=DEV, targets=["plate:col1"], volume=50, repetitions=1, specialized={"blow_out": True})
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)  # blow-out, mix, then aspirate
  assert all(v == pytest.approx(100.0) for v in plate_volumes(await ot2.call("get_state", device=DEV)).values())


async def test_a_new_tip_needs_no_preparing(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100, specialized={"blow_out": True})
  await ot2.call("drop_tips", device=DEV)
  await ot2.call("pick_up_tips", device=DEV)
  mark = len(robot_commands(ot2))
  out = await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  assert out["note"] is None and "prepareToAspirate" not in kinds(since(ot2, mark))


async def test_an_air_gap_is_drawn_at_the_well_top_and_pushed_out_by_the_next_dispense(ot2):
  await ready(ot2)
  mark = len(robot_commands(ot2))
  out = await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100, specialized={"air_gap_ul": 20})
  commands = since(ot2, mark)
  assert "drew 20 uL of air at the well top (airGapInPlace)" in out["note"]
  assert kinds(commands).count("aspirateInPlace") == 1 and kinds(commands).count("airGapInPlace") == 1
  gap = commands[kinds(commands).index("airGapInPlace")]
  assert gap["params"]["volume"] == 20 and gap["status"] == "succeeded"
  assert gap["params"]["flowRate"] == commands[kinds(commands).index("aspirateInPlace")]["params"]["flowRate"]
  state = await ot2.call("get_state", device=DEV)  # the air is not liquid
  assert state["liquids_ul"]["reagents"]["A1"] == pytest.approx(1700.0)
  mark = len(robot_commands(ot2))
  out = await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)
  commands = since(ot2, mark)
  assert "pushed out 20 uL of air" in out["note"]
  assert [c["params"]["volume"] for c in commands if c["commandType"] == "dispenseInPlace"] == [120]
  assert all(v == pytest.approx(100.0) for v in plate_volumes(await ot2.call("get_state", device=DEV)).values())
  assert await ot2.refused("dispense", device=DEV, targets=["plate:col2"], volumes=20) == "insufficient_in_tips"
  check = await ot2.call("verify", check="volumes_match", expected={"plate:A1": 100.0, "plate:H1": 100.0})
  assert check["verdict"] == "pass"


async def test_an_air_gap_stays_behind_the_liquid_aspirated_after_it(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100, specialized={"air_gap_ul": 20})
  await ot2.call("aspirate", device=DEV, targets=["stock"], volumes=100)  # [stock, air, diluent]
  mark = len(robot_commands(ot2))
  first = await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)
  second = await ot2.call("dispense", device=DEV, targets=["plate:col2"], volumes=100)
  moved = [c["params"]["volume"] for c in since(ot2, mark) if c["commandType"] == "dispenseInPlace"]
  assert moved == [100, 120] and first["note"] is None and "pushed out 20 uL of air" in second["note"]
  state = await ot2.call("get_state", device=DEV)
  assert all(v == pytest.approx(100.0) for v in plate_volumes(state).values())
  assert state["liquids_ul"]["reagents"]["A1"] == pytest.approx(1700.0)
  assert state["liquids_ul"]["reagents"]["A2"] == pytest.approx(400.0)


async def test_a_tip_holds_its_liquid_and_its_air(ot2):
  await ready(ot2)
  before = count(ot2)
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"], volumes=290,
                           specialized={"air_gap_ul": 20}) == "exceeds_tip"
  text = await ot2.call_text("aspirate", device=DEV, targets=["diluent"], volumes=290, specialized={"air_gap_ul": 20})
  assert "290 uL and 20 uL of air exceeds the mounted 300 uL tips" in text
  assert count(ot2) == before  # refused before anything moved
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=150, specialized={"air_gap_ul": 100})
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"], volumes=60) == "exceeds_tip"  # 150+100+60
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=50)  # exactly full
  assert await ot2.refused("mix", device=DEV, targets=["diluent"], volume=20, repetitions=1) == "exceeds_tip"
  state = await ot2.call("get_state", device=DEV)
  assert state["liquids_ul"]["reagents"]["A1"] == pytest.approx(1600.0)  # 200 drawn: the air is not in the twin


async def test_a_blow_out_must_leave_nothing_to_blow_out(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=150)
  before = count(ot2)
  text = await ot2.call_text("dispense", device=DEV, targets=["plate:col1"], volumes=100, specialized={"blow_out": True})
  assert "[blow_out_empties_tip]" in text and "still holds 50 uL" in text
  assert await ot2.refused("mix", device=DEV, targets=["diluent"], volume=50, repetitions=1,
                           specialized={"blow_out": True}) == "blow_out_empties_tip"
  assert count(ot2) == before  # nothing was sent
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100)
  await ot2.call("dispense", device=DEV, targets=["plate:col2"], volumes=50, specialized={"blow_out": True})  # empties it
  assert (await ot2.call("get_state", device=DEV))["liquids_ul"]["reagents"]["A1"] == pytest.approx(1650.0)


async def test_refusals_before_anything_moves(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=100)
  before = count(ot2)
  for params, code in (({"blow_out_height_mm": 6}, "out_of_range"), ({"touch_tip_radius": 0.4}, "out_of_range"),
                       ({"touch_tip_v_offset_mm": 1}, "out_of_range"), ({"blow_out": 1}, "bad_type"),
                       ({"blow_out_height_mm": 2}, "option_without_switch"),
                       ({"touch_tip_radius": 0.5}, "option_without_switch"), ({"jet": True}, "unknown_param")):
    assert await ot2.refused("dispense", device=DEV, targets=["plate:col1"], volumes=100, specialized=params) == code
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"], volumes=20, specialized={"air_gap_ul": 301}) == "out_of_range"
  assert await ot2.refused("aspirate", device=DEV, targets=["diluent"], volumes=20, specialized={"air_gap_ul": -1}) == "out_of_range"
  assert count(ot2) == before
  # the same value as the default is no option: a task limit fills defaults in
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=100, specialized={"touch_tip_radius": 1.0})


async def test_no_blow_out_or_touch_under_the_liquid(ot2):
  await ready(ot2)
  await ot2.call("aspirate", device=DEV, targets=["diluent"], volumes=300)
  before = count(ot2)
  # 300 uL fills a 6.86 mm well of a Corning flat plate about 8 mm deep; the well top is 10.2 mm
  text = await ot2.call_text("dispense", device=DEV, targets=["plate:col1"], volumes=300,
                             specialized={"blow_out": True, "blow_out_height_mm": -5})
  assert "[under_liquid]" in text and "the blow-out" in text
  assert await ot2.refused("dispense", device=DEV, targets=["plate:col1"], volumes=300,
                           specialized={"touch_tip": True, "touch_tip_v_offset_mm": -4}) == "under_liquid"
  assert count(ot2) == before
  await ot2.call("dispense", device=DEV, targets=["plate:col1"], volumes=300,
                 specialized={"blow_out": True, "touch_tip": True})  # at the top and 1 mm under it: above the liquid
  assert all(v == pytest.approx(300.0) for v in plate_volumes(await ot2.call("get_state", device=DEV)).values())
