"""The physical-world context: independent sources per verification point, the conclusion drawn from them, and what
the digital twin is not. Pure: no camera, no robot."""

from ahc import physical_world as pw
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.devices.spec import device_spec
from ahc.instrument_context import InstrumentContext
from ahc.physical_world import Observation

DECK = {"what": "slot:2", "level": "slot", "check": "deck_matches_layout"}
LIQUID = {"what": "plate:A1", "level": "well", "check": "liquid_present"}
TIPS = {"what": "tips:ot2.right", "level": "tip", "check": "tips_mounted"}


class FakeSource:
  """A context source in a few lines: it observes the points whose check it knows, at a level, fresh or not."""

  def __init__(self, name, levels, fresh=True, state="ok", kind="fake"):
    self.name, self.levels, self.fresh, self.state, self.kind = name, levels, fresh, state, kind
    self.asked = []

  def describe(self):
    return {"name": self.name}

  def observe(self, point, target):
    self.asked.append(point["what"])
    level = self.levels.get(point["check"])
    return [] if level is None else [Observation(self.name, level, self.fresh, self.state, None if self.state == "ok" else f"{self.name} is {self.state}")]


def run(points, *sources):
  return pw.evaluate(points, lambda what: {"key": what, "wells": None, "type": None}, list(sources))


def test_a_point_covered_by_two_sources_lists_both():
  result = run([DECK], FakeSource("vision:usb", {"deck_matches_layout": "well"}), FakeSource("vision:rs", {"deck_matches_layout": "slot"}))
  [row] = result["points"]
  assert row["status"] == "covered"
  assert row["sources"] == [{"source": "vision:usb", "level": "well", "fresh": True, "covers": True},
                            {"source": "vision:rs", "level": "slot", "fresh": True, "covers": True}]
  confidence = result["physical_world_confidence"]
  assert confidence["by_check"]["deck_matches_layout"]["sources"] == [{"source": "vision:rs", "level": "slot"}, {"source": "vision:usb", "level": "well"}]
  assert confidence["by_source"] == {"vision:usb": {"observes": 1, "covers": 1}, "vision:rs": {"observes": 1, "covers": 1}}
  assert confidence["all_confident"] is True


def test_one_source_at_the_needed_level_is_enough_whatever_the_others_say():
  weak = FakeSource("vision:rs", {"liquid_present": "slot"})  # sees the plate, not its wells
  stale = FakeSource("vision:top", {"liquid_present": "well"}, fresh=False, state="unclear")
  alone = run([LIQUID], weak, stale)["points"][0]
  assert alone["status"] == "unclear" and "the point needs well" in alone["why"]  # an unclear source beats a low one: capture again
  sensor = FakeSource("instrument:ot2", {"liquid_present": "well"})
  [row] = run([LIQUID], weak, stale, sensor)["points"]
  assert row["status"] == "covered" and [e["covers"] for e in row["sources"]] == [False, False, True]
  assert "why" not in row  # covered: nothing to explain


def test_a_point_nothing_observes_is_uncovered_and_there_is_no_score():
  result = run([DECK, TIPS], FakeSource("vision:usb", {"deck_matches_layout": "slot"}))
  assert {p["what"]: p["status"] for p in result["points"]} == {"slot:2": "covered", "tips:ot2.right": "uncovered"}
  tips = result["points"][1]
  assert tips["sources"] == [] and tips["why"] == "no source observes it"
  confidence = result["physical_world_confidence"]
  assert confidence["all_confident"] is False and set(confidence) == {"by_check", "by_source", "all_confident"}
  assert confidence["by_check"]["tips_mounted"] == {"status": "uncovered", "points": {"covered": 0, "uncovered": 1, "unclear": 0}, "sources": []}
  assert not run([]).get("points") and run([])["physical_world_confidence"]["all_confident"] is False  # nothing to be confident about


def test_unclear_wins_over_uncovered_in_the_conclusion_and_reasons_name_the_points():
  result = run([DECK, TIPS, {**LIQUID, "what": "plate:B1"}], FakeSource("vision:usb", {"deck_matches_layout": "slot"}, state="needs_new_map"))
  by = {p["what"]: p["status"] for p in result["points"]}
  assert by == {"slot:2": "unclear", "tips:ot2.right": "uncovered", "plate:B1": "uncovered"}
  assert result["points"][0]["sources"][0]["state"] == "needs_new_map" and any("map_camera again" in s for s in result["suggestions"])
  text = pw.reasons(result["points"])
  assert text.startswith("deck_matches_layout point slot:2 (level slot): vision:usb is needs_new_map; tips_mounted point tips:ot2.right")
  assert pw.reasons(run([DECK], FakeSource("a", {"deck_matches_layout": "slot"}))["points"]) is None


def test_reasons_are_capped():
  rows = [{"what": f"slot:{n}", "level": "slot", "check": "deck_matches_layout", "status": "uncovered", "sources": [], "why": "w"} for n in range(1, 9)]
  assert pw.reasons(rows).endswith("; and 3 more") and pw.reasons(rows).count(" point slot:") == 5


def test_the_digital_twin_is_never_a_source():
  """The twin knows every volume and tip; with no observing source the points stay uncovered, however well it matches."""
  twin_knows = {"volumes_ul": {"plate:A1": 100.0}, "tips_mounted": {"ot2.right": 8}}
  result = run([LIQUID, TIPS])  # no source at all; the twin's numbers are not an input anywhere
  assert {p["status"] for p in result["points"]} == {"uncovered"} and twin_knows
  assert not result["physical_world_confidence"]["all_confident"]
  assert result["physical_world_confidence"]["by_source"] == {}
  assert "digital twin" in (pw.__doc__ or "") and "NOT a source" in pw.__doc__


# -- the instrument as a source ---------------------------------------------------------------------

def test_the_ot2_reports_no_sensors():
  adapter = OT2Adapter(device_spec("opentrons.ot2"), "sim", {})
  assert adapter.sensor_report() == {"observes": {}, "note": "OT-2: no tip or liquid sensors"}
  context = InstrumentContext("ot2", adapter.sensor_report())
  assert context.describe() == {"device": "ot2", "observes": {}, "note": "OT-2: no tip or liquid sensors"}
  assert context.observe(TIPS, {"key": "tips:ot2.right"}) == [] and context.observe(LIQUID, {"key": "2"}) == []


def test_the_starlet_sim_reports_no_sensors_yet():
  adapter = STARletAdapter(device_spec("hamilton.starlet"), "sim", {})
  report = adapter.sensor_report()
  assert report["observes"] == {} and "not read as evidence yet" in report["note"] and "liquid-level" in report["note"]
  assert InstrumentContext("starlet", report).observe(LIQUID, {"key": "x"}) == []


def test_a_device_that_reports_a_sensor_covers_the_matching_points():
  """The extension point: an adapter's sensor_report names what it senses, and the instrument source starts covering it."""
  context = InstrumentContext("star", {"observes": {"tips_mounted": "tip"}, "note": "tip presence"})
  [seen] = context.observe(TIPS, {"key": "tips:star.pip"})
  assert seen == Observation("instrument:star", "tip") and context.observe(DECK, {"key": "2"}) == []
  result = run([TIPS, LIQUID], context)
  assert [p["status"] for p in result["points"]] == ["covered", "uncovered"]
  assert result["points"][0]["sources"] == [{"source": "instrument:star", "level": "tip", "fresh": True, "covers": True}]
