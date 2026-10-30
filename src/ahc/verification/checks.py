"""Physical-world verification: checks, evidence, verdicts.

The agent judges each check; this module gathers the evidence it judges from. In simulation the
simulator's state is compared with the digital twin and the verdict is marked simulated. On real
hardware a person verifies for now (placeholder until camera and CV verifiers are connected); photos from
capture can be attached to a check as evidence, and the person still judges.
volumes_match is the digital twin's own answer on every backend: what was sent, not what was measured.

The person's tips_mounted and liquid_present checks hold motion (the server enforces it, as it does the
deck check's gate):

  pending  nothing moves until the person answers (check_pending). A new check of the same pipette, or of
           the same containers, replaces the one waiting (check_superseded for the old one).
  failed   tips_mounted: only drop_tips moves, until a drop_tips on that pipette goes through or the person
           passes a later tips_mounted check of it. liquid_present: no pick-up and no liquid moves (drop_tips
           and load_layout do), until the person passes a later check of those containers or a new layout
           is loaded (check_failed).
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any, Callable

from ahc.devices.base import Adapter
from ahc.devices.spec import DeviceSpec
from ahc.core.errors import LabError
from ahc.records import RunLog
from ahc.verification.ask import DENIED, agent_now, pass_now

# What the person checks on a real deck besides each labware and its position: in the check itself, in what
# verify asks them, and where find_layout_references sends them to the deck.
ON_A_REAL_DECK = ("lids off, A1 at the back left of each slot, the liquids and their volumes, nothing else on the deck, "
                  "and no tips on either pipette")

CHECKS = {
  "tips_mounted": "Each channel in use carries a tip (expect='mounted') or none does (expect='none'). expect='none' "
                  "without a device: no tip on any pipette, in one check.",
  "liquid_present": "Each target holds at least min_volume_ul.",
  "deck_matches_layout": f"The physical deck matches the loaded layout: labware types and positions. On a real deck "
                         f"the person also checks: {ON_A_REAL_DECK}.",
  "volumes_match": "Each target's container holds the expected volume (expected={target: uL}) within tolerance_ul, "
                   "in the digital twin: what was sent, not measured.",
}

CAMERA_NOTE = ("no camera verifier: no image is judged automatically. Photos from capture can be attached with "
               "images=[...] as evidence; on real hardware the person still records the verdict")
TWIN_JUDGE = "digital twin (what was sent, not measured)"
AUTO_JUDGE = "agent (auto, cameras)"
HOLDING = ("tips_mounted", "liquid_present")  # the person's checks that hold motion while they wait, or once failed
# On real hardware the person checks the deck against this, every time.
DECK_ON_SITE = (f"The person on site checks the deck against the layout: every slot's labware as listed, {ON_A_REAL_DECK}. "
                "The layout is also on the run dashboard. ")


class Verification:
  def __init__(self, spec: DeviceSpec, adapter: Adapter, runlog: RunLog,
               unknown: Callable[[], dict[str, Any] | None] | None = None,
               tips_note: Callable[[], str | None] | None = None):
    self.spec = spec
    self.adapter = adapter
    self.runlog = runlog
    # What the twin cannot vouch for on real hardware now ({"why", ...}), or None: the server knows.
    self.unknown = unknown or (lambda: None)
    # Why tips may be on the pipettes that the robot (and so the twin) no longer knows of, or None: the server knows.
    self.tips_note = tips_note or (lambda: None)
    self.pending: dict[str, dict[str, Any]] = {}
    # The person's tips and liquid checks that hold motion, by check id: {"check", "state" ("pending" or "failed"),
    # "subject" (the pipette, None for every pipette, or the containers), "covers" (what a later pass or a drop
    # clears), "asked", "targets", "expect", and once failed "failed_at"}.
    self.holds: dict[str, dict[str, Any]] = {}
    self.superseded: dict[str, str] = {}  # a check a newer one of the same subject replaced -> that newer check

  async def verify(self, check: str, device: str | None = None, targets: list[str] | None = None,
                   expect: str = "mounted", min_volume_ul: float = 0.0, person: bool = False,
                   expected: dict[str, Any] | None = None, tolerance_ul: float = 0.5,
                   differs: str | None = None, images: list[str] | None = None, auto: bool = False) -> dict[str, Any]:
    """`person`: the verdict is the person's even in simulation (a layout no reference matches). `differs`: how
    the loaded layout differs from the task folder's references, for the person checking the deck. `images`:
    photos from capture (the server checked their paths) kept as the check's camera evidence. `auto` (real
    hardware, the server decided): the agent answers from camera images (agent_verdict) instead of the person;
    the check still holds motion while it waits, and stays a person check underneath: a fail or an unclear
    judgment hands it to them."""
    if check not in CHECKS:
      raise LabError("unknown_check", f"no check named {check!r}.", f"Checks: {', '.join(CHECKS)}.")
    if check == "volumes_match":
      twin, sensors, note = self._volumes(expected, tolerance_ul)
      verdict, judged_by = ("pass" if twin["as_expected"] else "fail"), TWIN_JUDGE
    else:
      twin, sensors, note = await self._evidence(check, device, targets, expect, min_volume_ul)
      if self.adapter.simulated and not person:
        passed = twin["as_expected"] and (sensors is None or sensors.get("agrees_with_twin", True))
        verdict, judged_by = ("pass" if passed else "fail"), "simulation"
      elif self.adapter.simulated:
        verdict, judged_by = "pending", "the person: a layout no reference matches"
      else:
        verdict, judged_by = "pending", "human (placeholder)"
    check_id = uuid.uuid4().hex[:8]
    result = {"check_id": check_id, "check": check, "verdict": verdict, "judged_by": judged_by,
              "simulated": self.adapter.simulated,
              "evidence": {"digital_twin": twin, "sensors": sensors,
                           "camera": {"images": list(images), "note": CAMERA_NOTE} if images else CAMERA_NOTE},
              "note": note}
    if check == "volumes_match" and twin["mismatches"]:
      result["mismatches"] = twin["mismatches"]
    replaced: list[str] = []
    if verdict == "pending":
      self.pending[check_id] = result
      result["ask_person"] = self._ask_person(check, device, targets, expect, min_volume_ul, twin)
      result["next"] = self._ask(check_id, check, device, targets, expect, min_volume_ul, person, differs)
      if auto and not self.adapter.simulated:
        result["auto"] = True
      if check in HOLDING:
        replaced = self._hold(check_id, check, device, twin, targets, expect, min_volume_ul)
        if replaced:
          result["supersedes"] = replaced[-1]
    for old in replaced:  # never answerable now: the run log (and so the dashboard) says so
      self.runlog.record({"type": "verification", "check_id": old, "check": check, "verdict": "superseded",
                          "judged_by": f"nobody: check {check_id} replaced it"})
    self.runlog.record({"type": "verification", **{k: result[k] for k in ("check_id", "check", "verdict", "judged_by")},
                        "evidence": result["evidence"], **({"supersedes": replaced[-1]} if replaced else {}),
                        **({"auto": True} if result.get("auto") else {})})
    return result

  def mark_auto(self, check_id: str) -> None:
    """A check that waited for the person is the agent's to judge now (the server found auto applies)."""
    result = self.pending[check_id]
    result["auto"] = True
    self.runlog.record({"type": "verification", "check_id": check_id, "check": result["check"], "verdict": "pending",
                        "judged_by": result["judged_by"], "evidence": result["evidence"], "auto": True})

  def hand_to_person(self, check_id: str, judgment: str) -> dict[str, Any]:
    """An auto check the agent could not pass (fail, unclear, or auto no longer applies): it is a plain person check
    from now on, as verify first made it (still pending, motion held). Nothing is recorded as a verdict; the run log
    says it waits for the person, with `judgment` (what the agent said) for the dashboard. Returns the pending
    result."""
    result = self.pending[check_id]
    result.pop("auto", None)
    self.runlog.record({"type": "verification", "check_id": check_id, "check": result["check"], "verdict": "pending",
                        "judged_by": f"the person: {judgment}", "evidence": result["evidence"]})
    return result

  def _ask(self, check_id, check, device, targets, expect, min_volume_ul, person, differs=None) -> str:
    """What the person is asked, check by check: the call that asks them is the next one."""
    homes = check == "tips_mounted" and device is None and not self.adapter.simulated  # the dialog before load_layout
    now = f" {pass_now(check_id, homes=homes)} {DENIED}"
    if person and self.adapter.simulated:
      return ((differs or "This layout matches no reference in this task folder.") + " So the person confirms it: "
              "ask_person lists the labware, positions and liquids (also on the run dashboard)." + now)
    if check == "deck_matches_layout":
      return DECK_ON_SITE + (f"{differs} " if differs else "") + now.lstrip()
    return f"The person on site checks {self._question(check, device, targets, expect, min_volume_ul)}.{now}"

  def _ask_person(self, check, device, targets, expect, min_volume_ul, twin) -> str:
    """What the person confirms by approving record_verdict, as one plain sentence: the note of that call."""
    if check == "deck_matches_layout":
      deck = f"The deck matches the loaded layout: {self.adapter.require_layout().describe()}"
      return deck + (f"; {ON_A_REAL_DECK}." if not self.adapter.simulated else ".")
    if check == "tips_mounted" and device is None:
      names = list(self._pipettes())
      return f"No tip on {'either' if len(names) == 2 else 'any'} pipette ({', '.join(names)})."
    if check == "tips_mounted":
      address = self._address(device)
      if expect == "none":
        return f"No tip on {address}."
      n = twin.get("tips_mounted")
      return (f"{n} tip{'s' if n != 1 else ''} seated straight on {address}." if isinstance(n, int) and n > 0 else
              f"A tip on every channel in use of {address}, seated straight.")
    least = f"at least {min_volume_ul:g} uL" if min_volume_ul > 0 else "some liquid"
    return f"Each container of {', '.join(targets or [])} holds {least}."

  def _question(self, check, device, targets, expect, min_volume_ul) -> str:
    """What the person checks in a tips or liquid check, as one clause."""
    if check == "tips_mounted" and device is None:
      return f"every pipette ({', '.join(self._pipettes())}): no tip on any channel; they take any off by hand first"
    if check == "tips_mounted":
      return f"{device}: " + ("a tip on every channel in use, seated straight" if expect == "mounted" else
                              "no tip on any channel")
    return f"that each container of {', '.join(targets or [])} holds at least {min_volume_ul:g} uL"

  # -- what the person's checks hold back ---------------------------------------------------------

  def _answer(self, check_id: str) -> str:
    """The call that answers a pending check: the agent's for an auto check, else the person's."""
    return agent_now(check_id) if self.pending.get(check_id, {}).get("auto") else pass_now(check_id)

  def _hold(self, check_id: str, check: str, device: str | None, twin: dict[str, Any], targets: list[str] | None,
            expect: str, min_volume_ul: float) -> list[str]:
    """The person's new tips or liquid check holds motion until they answer. A check of the same subject that still
    waits (the same pipette, every pipette, or the same containers) is replaced, so no hold is left that nobody can
    answer; the check of every pipette also replaces the checks of single pipettes that wait. Returns the ids
    replaced."""
    if check == "tips_mounted":
      subject = None if device is None else self._address(device)
      covers = list(twin.get("pipettes") or {}) if subject is None else [subject]
    else:
      covers = sorted(twin.get("volumes_ul") or {})
      subject = tuple(covers)
    replaced = [cid for cid, h in self.holds.items() if h["state"] == "pending" and h["check"] == check
                and (h["subject"] == subject or (check == "tips_mounted" and subject is None and h["subject"] in covers))]
    for cid in replaced:
      del self.holds[cid]
      self.pending.pop(cid, None)
      self.superseded[cid] = check_id
    self.holds[check_id] = {"check": check, "state": "pending", "subject": subject, "covers": covers,
                            "asked": self._question(check, subject if check == "tips_mounted" else device, targets,
                                                    expect, min_volume_ul),
                            "targets": list(targets or []), "expect": expect}
    return replaced

  def _address(self, device: str) -> str:
    return f"{self.spec.prefix}.{self.spec.component(device).name}"

  def _settle(self, check_id: str, verdict: str) -> None:
    """The person's verdict on a holding check: a fail keeps it holding (failed); a pass clears it, and a failed
    tips check of the pipettes it covers, or a failed liquid check of containers it covers, all of them."""
    hold = self.holds.get(check_id)
    if hold is None:
      return
    if verdict == "fail":
      hold["state"], hold["failed_at"] = "failed", time.strftime("%H:%M")
      return
    del self.holds[check_id]
    covered = set(hold["covers"])
    for cid, other in list(self.holds.items()):
      if other["state"] != "failed" or other["check"] != hold["check"]:
        continue
      if other["check"] == "tips_mounted":
        other["covers"] = [c for c in other["covers"] if c not in covered]
        if not other["covers"]:
          del self.holds[cid]
      elif covered >= set(other["covers"]):
        del self.holds[cid]

  def tips_dropped(self, device: str) -> None:
    """A drop_tips on this pipette went through: a failed tips check holds it back no longer."""
    address = self._address(device)
    for cid, hold in list(self.holds.items()):
      if hold["state"] == "failed" and hold["check"] == "tips_mounted" and address in hold["covers"]:
        hold["covers"] = [c for c in hold["covers"] if c != address]
        if not hold["covers"]:
          del self.holds[cid]

  def tips_found(self) -> dict[str, str]:
    """The pipettes the person found tips on that the twin may not know of (a tips check that asked for none, which
    they failed), each with that check; until a drop on it goes through or a later check of it passes."""
    found: dict[str, str] = {}
    for cid, hold in self.holds.items():
      if hold["state"] == "failed" and hold["check"] == "tips_mounted" and hold["expect"] == "none":
        for address in hold["covers"]:
          found.setdefault(address, f"the person failed tips_mounted check {cid} at {hold['failed_at']}")
    return found

  def layout_loaded(self) -> str:
    """A new layout: a failed liquid check no longer describes the deck. Returns what the person found in it, for the
    deck check of the new layout to name ("" when nothing)."""
    cleared = [(cid, h) for cid, h in self.holds.items() if h["state"] == "failed" and h["check"] == "liquid_present"]
    for cid, _ in cleared:
      del self.holds[cid]
    return " ".join(f"Before this load the person failed liquid_present check {cid} (they checked {h['asked']}): they "
                    "check those liquids against this layout too." for cid, h in cleared)

  def _until(self, hold: dict[str, Any]) -> str:
    """What a failed check holds back, and until when, as one sentence."""
    if hold["check"] == "tips_mounted":
      covers = hold["covers"]
      until = (f"a drop_tips on {covers[0]} goes through, or the person passes a later tips_mounted check of it"
               if len(covers) == 1 else
               f"each of {', '.join(covers)} has had a drop_tips go through, or passed a later tips_mounted check")
      return f"Only drop_tips moves until {until}."
    return (f"No tips are picked up and no liquid moves until the person passes a later liquid_present check of "
            f"{', '.join(hold['targets'])}, or a new layout is loaded; drop_tips and load_layout still go ahead.")

  def held_after(self, check_id: str) -> str:
    """What the person's failed check now holds back ("" when it holds nothing)."""
    hold = self.holds.get(check_id)
    return self._until(hold) if hold is not None and hold["state"] == "failed" else ""

  def require_unheld(self, op: str) -> None:
    """Refuse motion a person's check holds back: anything while one waits for their verdict (check_pending);
    after a failed tips check all but drop_tips, after a failed liquid check all but drop_tips and load_layout
    (check_failed)."""
    waiting = {cid: h for cid, h in self.holds.items() if h["state"] == "pending"}
    if waiting:
      named = " and ".join(f"{h['check']} check {cid} (they check {h['asked']})" for cid, h in waiting.items())
      auto = all(self.pending.get(c, {}).get("auto") for c in waiting)
      raise LabError("check_pending",
                     f"{named} {'waits' if len(waiting) == 1 else 'wait'} for "
                     + ("the agent's judgment of the camera images, so nothing moves until it comes." if auto else
                        "the person's verdict, so nothing moves until they answer."),
                     f"{' '.join(self._answer(cid) for cid in waiting)}{'' if auto else ' Never a verdict of your own.'} "
                     "A new verify of the same check replaces the one waiting.")
    failed = {cid: h for cid, h in self.holds.items() if h["state"] == "failed"}
    for cid, hold in failed.items():
      if hold["check"] == "tips_mounted" and op != "drop_tips":
        every = hold["subject"] is None
        doubt = (self.tips_note() or "") if every else ""
        raise LabError("check_failed",
                       f"the person failed tips_mounted check {cid} (they checked {hold['asked']}). "
                       f"{self._until(hold)}{' ' + doubt if doubt else ''}",
                       "Ask the person what they see. Where the robot still holds tips, drop_tips; the person takes "
                       "any others off by hand; then verify(check='tips_mounted', expect='none') without device again "
                       "and record_verdict with its ask_person." if every else
                       "Ask the person what they see. If the robot still holds tips on it (get_state), drop_tips, also "
                       "for tips that sit crooked; if it holds none, the person takes any off by hand and you "
                       f"verify(check='tips_mounted', device='{hold['subject']}', expect='none'), then record_verdict "
                       "with its ask_person.")
    for cid, hold in failed.items():
      if hold["check"] == "liquid_present" and op not in ("drop_tips", "load_layout"):
        raise LabError("check_failed",
                       f"the person failed liquid_present check {cid} (they checked {hold['asked']}). "
                       f"{self._until(hold)}",
                       "Ask the person what they see. Once they have put it right, verify(check='liquid_present') of "
                       "those targets again and record_verdict with its ask_person, or load_layout again with the "
                       "volumes they report.")

  def holding(self, skip: str | None = None) -> str:
    """What the person's checks hold back now, for lab_overview's next ("" when nothing); `skip`: a check named
    elsewhere in it."""
    parts = []
    for cid, hold in self.holds.items():
      if cid == skip:
        continue
      if hold["state"] == "pending":
        by = "the agent's judgment of the camera images" if self.pending.get(cid, {}).get("auto") else "the person's verdict"
        parts.append(f"{hold['check']} check {cid} waits for {by} (checking {hold['asked']}): "
                     f"{self._answer(cid)} Nothing moves before that.")
      else:
        parts.append(f"The person failed {hold['check']} check {cid}: {self._until(hold)}")
    return " ".join(parts) + (" " if parts else "")

  def _volumes(self, expected: Any, tolerance_ul: Any):
    """volumes_match: each named container against the twin, per container."""
    if not isinstance(expected, dict) or not expected:
      raise LabError("expected_required", "volumes_match needs expected={target: uL, ...}.",
                     "e.g. expected={'plate:A2:H2': 100, 'plate:A3:H3': 100}; any target form works.")
    if isinstance(tolerance_ul, bool) or not isinstance(tolerance_ul, (int, float)) or not math.isfinite(tolerance_ul) \
        or tolerance_ul < 0:
      raise LabError("bad_tolerance", f"tolerance_ul must be a number of uL >= 0, got {tolerance_ul!r}.")
    layout = self.adapter.require_layout()
    wanted: dict[str, float] = {}
    containers: dict[str, Any] = {}
    for target, volume in expected.items():
      if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume < 0:
        raise LabError("bad_expected", f"expected[{target!r}] must be a volume in uL >= 0, got {volume!r}.")
      for container in layout.resolve_targets([target]):
        if container.name in wanted and abs(wanted[container.name] - volume) > 1e-9:
          raise LabError("bad_expected", f"{container.name} is expected to hold both {wanted[container.name]:g} and "
                         f"{volume:g} uL.", "Name each container once, or with one volume.")
        wanted[container.name], containers[container.name] = float(volume), container
    doubt = self.unknown()  # real hardware after an unknown outcome: a well that call touched never matches
    unknown = {id(w) for w in doubt["wells"]} if doubt else set()
    rows = {}
    for name, volume in wanted.items():
      if id(containers[name]) in unknown:
        rows[name] = {"expected": volume, "twin": "unknown", "matches": False}
        continue
      held = round(containers[name].tracker.get_used_volume(), 3)
      rows[name] = {"expected": volume, "twin": held, "matches": abs(held - volume) <= tolerance_ul + 1e-9}
    mismatches = [{"container": name, "expected": r["expected"], "twin": r["twin"]} for name, r in rows.items()
                  if not r["matches"]]
    twin = {"containers": rows, "tolerance_ul": tolerance_ul, "mismatches": mismatches, "as_expected": not mismatches}
    note = ("The digital twin holds what was sent to the device, not a measurement: on real hardware the person (or a "
            "camera) confirms what is really in the wells.")
    if any(r["twin"] == "unknown" for r in rows.values()):
      note = f"{doubt['why']} {note}"
    return twin, None, note

  async def _evidence(self, check, device, targets, expect, min_volume_ul):
    if check == "deck_matches_layout":
      layout = self.adapter.require_layout()
      comparison = layout.compare_placements()
      note = ("The device model's deck tree is compared with what load_layout placed there. On real hardware "
              "that is not enough: the person on site checks the deck (a camera later).")
      return ({**comparison, "layout": layout.summary()}, None, note)
    if check == "tips_mounted":
      if device is None and expect == "none":
        return await self._every_pipette()
      if device is None:
        raise LabError("device_required", "tips_mounted needs a device component.",
                       f"Name the component ({', '.join(self._pipettes())}); expect='none' without a device checks "
                       "every pipette at once.")
      comp = self.spec.component(device)
      if expect not in ("mounted", "none"):
        raise LabError("bad_expect", "expect must be 'mounted' or 'none'.")
      doubt = self._tips_unknown()
      if doubt is not None:  # never "0 tips" as if known: the person checks
        twin = {"tips_mounted": "unknown", "expect": expect, "as_expected": None, "why": doubt["why"]}
      else:
        mounted = len(self.adapter.mounted_tips(comp))
        twin = {"tips_mounted": mounted, "expect": expect,
                "as_expected": (mounted > 0) if expect == "mounted" else (mounted == 0)}
      reading = await self.adapter.sense(comp, check)
      if reading is not None:
        sensed = sum(1 for p in reading["tip_presence_per_channel"] if p)
        reading = {**reading, "sensed_tips": sensed}
        if doubt is None:
          reading["agrees_with_twin"] = sensed == twin["tips_mounted"]
      note = None if reading is not None else "this device has no tip sensor; a camera is needed"
      if doubt is not None:
        note = doubt["why"] + (f" ({note})" if note else "")
      return twin, reading, note
    if check == "liquid_present":
      containers = self.adapter.require_layout().resolve_targets(targets or [])
      if not containers:
        raise LabError("targets_required", "liquid_present needs targets.")
      doubt = self.unknown()
      unknown = {id(w) for w in doubt["wells"]} if doubt else set()
      volumes = {c.name: "unknown" if id(c) in unknown else c.tracker.get_used_volume() for c in containers}
      known = [v for v in volumes.values() if v != "unknown"]
      twin = {"volumes_ul": volumes, "min_volume_ul": min_volume_ul,
              "as_expected": all(v >= min_volume_ul and v > 0 for v in known) if len(known) == len(volumes) else None}
      note = "liquid-level sensing is not wired in this prototype"
      return twin, None, note if len(known) == len(volumes) else f"{doubt['why']} ({note})"
    raise AssertionError(check)

  def _tips_unknown(self) -> dict[str, Any] | None:
    """The tips the twin cannot vouch for: after an unknown outcome or a stop, or tips another run left on."""
    doubt = self.unknown()
    if doubt is None:
      noted = self.tips_note()
      doubt = {"why": noted} if noted else None
    return doubt

  def _pipettes(self) -> dict[str, Any]:
    return {f"{self.spec.prefix}.{c.name}": c for c in self.spec.components.values()
            if "liquid_handling" in c.capabilities}

  async def _every_pipette(self):
    """tips_mounted with expect='none' and no device: no tip on any channel of any pipette, one check for all
    (the person's word that the pipettes are empty). It needs no layout. On real hardware nothing is sent to the
    device, not even a sensor reading: the person on site is the evidence. It is for tips the robot no longer knows
    of: tips the live run holds are dropped (drop_tips), never taken off by hand behind the robot's back."""
    if not self.adapter.simulated and self.unknown() is None:
      on = ", ".join(f"{n} on {address}" for address, comp in self._pipettes().items()
                     if (n := len(self.adapter.mounted_tips(comp))))
      if on:
        raise LabError("tips_mounted",
                       f"{on} in this run, and the robot knows them: the check of every pipette asks the person to take "
                       "tips off by hand, which is only for tips the robot no longer knows of.",
                       "Call drop_tips first (discard or return), then check again; verify(check='tips_mounted', "
                       "device=...) checks how one pipette's tips sit.")
    doubt = self._tips_unknown()
    pipettes: dict[str, Any] = {}
    readings: dict[str, Any] = {}
    for address, comp in self._pipettes().items():
      pipettes[address] = "unknown" if doubt is not None else len(self.adapter.mounted_tips(comp))
      reading = await self.adapter.sense(comp, "tips_mounted") if self.adapter.simulated else None
      if reading is not None:
        readings[address] = {**reading, "sensed_tips": sum(1 for p in reading["tip_presence_per_channel"] if p)}
    known = [n for n in pipettes.values() if n != "unknown"]
    twin = {"pipettes": pipettes, "expect": "none",
            "as_expected": all(n == 0 for n in known) if len(known) == len(pipettes) else None}
    sensors = None
    if readings:
      sensors = {"pipettes": readings,
                 "agrees_with_twin": all(r["sensed_tips"] == pipettes[a] for a, r in readings.items())}
    note = None if readings else "no tip sensor read for this check; a camera is needed"
    if doubt is not None:
      twin["why"] = doubt["why"]
      note = doubt["why"] + (f" ({note})" if note else "")
    return twin, sensors, note

  def _waiting(self, check_id: str) -> dict[str, Any]:
    """The pending check with this id, or the refusal that says why none waits."""
    if check_id in self.superseded:
      newest = self.superseded[check_id]
      while newest in self.superseded:
        newest = self.superseded[newest]
      raise LabError("check_superseded",
                     f"check {check_id} was replaced by a new verify of the same check, so a verdict on it would change "
                     "nothing.",
                     (f"Check {newest} waits for the person's verdict: " + pass_now(newest, "<its ask_person>")
                      if newest in self.pending and not self.pending[newest].get("auto") else
                      f"Check {newest} waits: " + agent_now(newest) if newest in self.pending else
                      "Run verify again, then record_verdict for that new check with its ask_person."))
    if check_id not in self.pending:
      raise LabError("unknown_check_id",
                     f"no check {check_id!r} waits for the person's verdict on this server. Simulated checks are judged by "
                     "the simulator (except a layout no reference matches), and a check made before the server restarted is gone.",
                     "Run verify again for a new check (after a restart: load_layout first); for a layout no reference "
                     "matches, record_verdict for the check verify gives, with its ask_person.")
    return self.pending[check_id]

  def record_verdict(self, check_id: str, verdict: str, note: str = "") -> dict[str, Any]:
    self._waiting(check_id)
    if verdict not in ("pass", "fail"):
      raise LabError("bad_verdict", "verdict must be 'pass' or 'fail'.")
    result = {**self.pending.pop(check_id), "verdict": verdict, "judged_by": "human", "note": note}
    for key in ("next", "ask_person", "supersedes", "auto"):  # what the person was asked; the caller says what comes next
      result.pop(key, None)
    self._settle(check_id, verdict)
    self.runlog.record({"type": "verification", "check_id": check_id, "check": result["check"],
                        "verdict": verdict, "judged_by": "human", "note": note})
    return result

  def agent_pass(self, check_id: str, note: str, images: list[str], per_point: list[dict[str, str]]) -> dict[str, Any]:
    """An auto check the agent judged a pass from camera images: it passes, judged by the agent, with the images as
    its evidence. The caller checked that auto still applies. Only a pass is ever recorded this way."""
    pending = self._waiting(check_id)
    if not pending.get("auto"):
      raise AssertionError("agent_pass on a check verify did not mark auto")
    self.pending.pop(check_id)
    camera = {"images": list(images), "per_point": per_point,
              "note": "judged by the agent from these images (auto-verify); listed on the dashboard for the person to review"}
    result = {**pending, "verdict": "pass", "judged_by": AUTO_JUDGE, "note": note,
              "evidence": {**pending["evidence"], "camera": camera}}
    for key in ("next", "ask_person", "supersedes", "auto"):
      result.pop(key, None)
    self._settle(check_id, "pass")
    self.runlog.record({"type": "verification", "check_id": check_id, "check": result["check"], "verdict": "pass",
                        "judged_by": AUTO_JUDGE, "note": note, "evidence": {"camera": camera}})
    return result
