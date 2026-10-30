"""Opentrons OT-2: driven step by step through PyLabRobot 1.0 over the robot-server HTTP API."""

from __future__ import annotations

import asyncio
import time
import urllib.error
from typing import Any

from pylabrobot.io.http import HTTPError
from pylabrobot.opentrons.tracking import track_liquid_transfer
from pylabrobot.resources import Container, OTDeck, TipSpot
from pylabrobot.resources.coordinate import Coordinate

from ahc.core.errors import LabError
from ahc.core.layout import Layout
from ahc.devices.opentrons.brand import OpentronsAdapter, ensure_tip_rack_definitions
from ahc.devices.spec import Component

try:  # PyLabRobot's own table of the tip sizes each OT-2 pipette takes, by its maximum volume: only to name them
  from pylabrobot.opentrons.ot2.pipette import _COMPATIBLE_TIP_CAPACITIES as _TIP_SIZES
except ImportError:  # a PyLabRobot without it: refusals name no size, and tip_fits still asks the pipette
  _TIP_SIZES = {}

_ENDED = ("stopped", "succeeded", "failed")  # run statuses after which the robot runs none of its commands
STOP_TIMEOUT_S = 30.0  # how long the emergency stop waits for the robot to report the run stopped
RECORD_KEPT = 12  # the last commands sent, kept so the robot can be asked what became of them
TOUCH_TIP_SPEED_MM_S = 60.0  # Opentrons' own touch_tip speed (its API clamps it to 20..80 on an OT-2)
# A specialized parameter that only qualifies another: refused when given a non-default value without it.
_NEEDS = {"blow_out_flow_rate_ul_s": "blow_out", "blow_out_height_mm": "blow_out",
          "touch_tip_v_offset_mm": "touch_tip", "touch_tip_radius": "touch_tip"}


def _close_transport(device) -> None:
  """PyLabRobot's HTTP transport, closed without touching the run. Blocking: callers use a thread."""
  asyncio.run(device.io.stop())


class OT2Adapter(OpentronsAdapter):
  """An OT-2 driven step by step through PyLabRobot 1.0 over the robot-server HTTP API."""

  async def start(self) -> None:
    await super().start()
    self.device = None
    self._setting_up = None  # the OT2 whose setup() runs now: the emergency stop reaches its run too
    self._sent = 0  # run commands this adapter submitted, over all its runs
    self._recent: list[dict[str, Any]] = []  # the last RECORD_KEPT of them: run, command type, id
    self._reset_tips()

  def _reset_tips(self, mount: str | None = None) -> None:
    """Forget what the tips hold beyond the twin's liquid (the air gaps) and which plungers a blow-out left
    past their bottom, for one mount or all: a new tip, a dropped one, or a new run."""
    if mount is None:
      self._tip_air: dict[str, list[list]] = {}  # per mount, from the tip opening inward: [kind, uL], kind air | liquid
      self._unprimed: set[str] = set()  # mounts whose plunger a blow-out left past the bottom
    else:
      self._tip_air.pop(mount, None)
      self._unprimed.discard(mount)

  def command_count(self) -> int:
    """Run commands submitted to the robot-server, counted here: no request to the robot."""
    return self._sent

  def run_id(self) -> str | None:
    """The robot-server run this adapter holds (also one the emergency stop halted): no request to the robot."""
    return getattr(getattr(self.device, "_run", None), "id", None)

  def _count_submissions(self, device) -> None:
    submit = device._api.submit_command

    async def counted(run_id, command_type, *args, **kwargs):
      self._sent += 1  # counted before the POST: from here on the robot may have it
      sent = {"run": run_id, "command": command_type, "id": None}
      self._recent = (self._recent + [sent])[-RECORD_KEPT:]
      sent["id"] = await submit(run_id, command_type, *args, **kwargs)
      return sent["id"]

    device._api.submit_command = counted

  def robot_record(self, n: int) -> list[dict[str, Any]]:
    """What the robot says became of the last n commands sent (at most RECORD_KEPT). Blocking; once the
    robot does not answer, the rest are not asked."""
    out: list[dict[str, Any]] = []
    reachable = True
    for sent in self._recent[-n:] if n > 0 else []:
      entry: dict[str, Any] = {"command": sent["command"]}
      if sent["id"] is None:
        entry["status"] = "unknown: the robot returned no command id"
      elif not reachable:
        entry["status"] = "unknown: the robot did not answer"
      else:
        try:
          data = self._get(f"/runs/{sent['run']}/commands/{sent['id']}", timeout=3).get("data") or {}
          entry["status"] = data.get("status")
          error = data.get("error") or {}
          if error:
            entry["error"] = error.get("detail") or error.get("errorType")
        except Exception as exc:  # noqa: BLE001 - the record is evidence, never a new failure
          entry["status"] = f"unknown: {type(exc).__name__}"
          reachable = not isinstance(exc, OSError) or isinstance(exc, urllib.error.HTTPError)
      out.append(entry)
    return out

  # -- runs on the robot ------------------------------------------------------------------------

  def _run_ended(self, run_id: str) -> bool:
    """The robot runs none of this run's commands any more: stopped or finished, gone (404), or no
    longer current (the robot restarted, or another run replaced it). Blocking."""
    try:
      run = self._get(f"/runs/{run_id}").get("data") or {}
    except urllib.error.HTTPError as exc:
      if exc.code == 404:
        return True
      raise
    return run.get("current") is False or run.get("status") in _ENDED

  def _replaced_by(self, run_id: str) -> dict[str, Any] | None:
    """The run current on the robot in place of this one (another client's, such as the Opentrons App's),
    or None: this run is still the current one, or none is (the robot restarted). Blocking."""
    try:
      run = self._get(f"/runs/{run_id}").get("data") or {}
    except urllib.error.HTTPError as exc:
      if exc.code != 404:
        raise
      run = {}  # gone
    if run.get("current") is True:
      return None
    body = self._get("/runs")
    for other in body.get("data") or []:
      if other.get("current") is True and other.get("id") != run_id:
        return {"id": other.get("id"), "status": other.get("status")}
    href = ((body.get("links") or {}).get("current") or {}).get("href") or ""
    current = href.rstrip("/").rpartition("/")[2]
    return {"id": current, "status": None} if current and current != run_id else None

  def _stop_run(self, run_id: str, timeout: float = STOP_TIMEOUT_S) -> dict[str, Any] | None:
    """Post the run's stop action on a connection of its own and wait until the robot reports the run
    stopped. Blocking; raises when that cannot be confirmed. Returns the run that is current on the robot
    in place of this one (another client's, or AHC's own next run), else None."""
    try:
      self._post(f"/runs/{run_id}/actions", {"data": {"actionType": "stop"}})
    except urllib.error.HTTPError as exc:
      if exc.code not in (404, 409):  # no such run, or not the current run: its state says which
        raise
    deadline = time.monotonic() + timeout
    while not self._run_ended(run_id):
      if time.monotonic() >= deadline:
        raise TimeoutError(f"the robot did not report run {run_id} stopped within {timeout:g} s")
      time.sleep(0.1)
    return self._replaced_by(run_id)

  async def _ended(self, device) -> bool:
    run = device._run
    if run is None or not run.active:
      return True
    try:
      return await asyncio.to_thread(self._run_ended, run.id)
    except Exception:  # noqa: BLE001 - the robot cannot say: not ended as far as we know
      return False

  async def _release(self, device) -> None:
    """Stop a device's run and close its connection. A run the robot already ended (the emergency stop, a
    restart, another run) is only let go; one that may still be active raises, so a retry can stop it."""
    if not self.halted:
      try:
        await device.stop()
        return
      except Exception:
        if not await self._ended(device):
          raise
    await asyncio.to_thread(_close_transport, device)

  async def stop(self) -> None:
    if self.device is not None:
      await self._release(self.device)
      self.device = None

  async def halt(self) -> bool:
    """The emergency stop: stop the robot's run at once, also a run load_layout is still setting up.

    Not device.stop(): that waits for the command in flight to give up PyLabRobot's operation lock,
    and PyLabRobot's transport sends one request at a time (a home holds it). The stop goes out on a
    connection of its own. True once the robot reports the run stopped, or no longer current with no
    other run in its place (the robot restarted); False when no run is active; raises when the stop
    cannot be confirmed, or another client's run replaced AHC's (other_run_active): halt_requested
    stays set, halted does not."""
    device = self.device if self.device is not None else self._setting_up
    run = getattr(device, "_run", None)
    if run is None or not run.active:
      return False
    self.halt_requested = True
    other = await asyncio.to_thread(self._stop_run, run.id)
    # While the robot was stopping this run, the person may have released the stop and the layout been
    # loaded again: halted then would describe that newer run, which nobody stopped.
    if not all(held is None or held is device for held in (self.device, self._setting_up)):
      return True
    if other is not None:  # another client took the robot over: it may be moving under that client's run
      status = f" (status {other['status']})" if other.get("status") else ""
      raise LabError("other_run_active",
                     f"AHC's run {run.id} is no longer the robot's current run: run {other['id']}{status}, which "
                     "AHC did not start, replaced it (the Opentrons App or another client). AHC did not stop that "
                     "run, so the robot may still be moving under it.",
                     "The robot's own controls (the Opentrons App) or the power switch stop it; AHC never stops a "
                     "run it did not start.")
    self.halted = True
    return True

  def _mount_hint(self) -> str:
    if self.simulated:
      return ("Restart this task's simulator (ahc-sim ot2 in a background shell): it takes its mounts from the "
              "config. Then load the layout again.")
    return ("The pipettes changed since the person confirmed the config: stop and tell the person. Write the config "
            "again with configure_devices (it reads the pipettes) and the person confirms it; then load the layout again.")

  # -- layout ---------------------------------------------------------------------------------

  async def load_layout(self, layout: dict[str, Any]) -> Layout:
    if self.device is not None and any(self.mounted_tips(c) for c in self.spec.components.values()):
      raise LabError("tips_mounted", "cannot change the layout with tips mounted.",
                     "Call drop_tips first.")
    from pylabrobot.opentrons import OT2

    await asyncio.to_thread(self.check_reachable)
    # The mounts before anything moves: setup() homes the robot, so a wrong robot must not get that far.
    mounts = (await asyncio.to_thread(self.identity))["mounts"]
    if not mounts["ok"]:
      raise LabError("mount_mismatch", "; ".join(mounts["problems"]) + "; nothing was moved.", self._mount_hint())
    types = {item.get("type") for item in (layout.get("slots") or {}).values() if isinstance(item, dict)}
    await asyncio.to_thread(ensure_tip_rack_definitions, types)  # never a download on the event loop
    deck = OTDeck()
    new_layout = self._build_deck(deck, layout)
    await self.stop()
    self.layout = None  # the old run and its deck are gone
    self.halted = self.halt_requested = False  # a stop from here on is the new run's
    device = OT2(host=self.host, port=self.port, deck=deck)
    self._count_submissions(device)
    self._setting_up = device
    try:
      await device.setup()
    finally:
      self._setting_up = None
    if self.halt_requested:  # the emergency stop reached this run while it was being set up
      try:
        await self._release(device)
      except Exception:  # noqa: BLE001 - the halt's own record says what is known
        pass
      raise LabError("device_halted", "the emergency stop stopped the robot's run while the layout was loading.",
                     "Once the person has released it and checked the deck, load the layout again.")
    if device._run.software_version.startswith("0.0.0"):
      # A dev robot-server (the simulator, also when rehearsing the robot backend against it) has no
      # /etc/VERSION.json and reports 0.0.0.dev0, which sends PyLabRobot down its pre-7.1
      # fixed-trash path. A real robot always reports its own release, so this never fires there.
      device._run._software_version = "8.8.2"
    for mount, name in self.spec.mounted.items():
      pipette = getattr(device, f"{mount}_pipette")
      if getattr(pipette, "name", None) != name:  # changed since the check above
        try:
          await device.stop()
        except Exception:  # noqa: BLE001 - the mismatch is what the person must hear about
          pass
        found = getattr(pipette, "name", None)
        raise LabError("mount_mismatch",
                       f"{mount} mount has {found!r}, the confirmed config names {name!r}." if found and name else
                       f"{mount} mount has {found!r}, the confirmed config names it empty." if found else
                       f"{mount} mount is empty, the confirmed config names {name!r}.", self._mount_hint())
    self.device = device
    self._reset_tips()
    self.outcome_unknown = None  # a new run: the person checked what the last one left
    self.layout = new_layout
    return new_layout

  def view_root(self):
    return self.device.deck if self.device is not None else None

  # -- liquid handling ------------------------------------------------------------------------

  def _pipette(self, comp: Component):
    if self.device is None:
      raise LabError("no_layout", "the OT-2 is not set up.", "Call load_layout first.")
    self.require_motion()
    return getattr(self.device, f"{comp.name}_pipette")

  def _mounted(self, comp: Component):
    """PyLabRobot's pipette on comp's mount, or None before a layout set the robot up."""
    return getattr(self.device, f"{comp.name}_pipette", None) if self.device is not None else None

  def tip_fits(self, comp: Component, rack: Any) -> bool:
    """By PyLabRobot's own rule for that pipette (a P20 takes 10 and 20 uL tips, a P300 200 and 300 uL)."""
    pipette = self._mounted(comp)
    tip = next((spot.tip for spot in rack.get_all_items() if spot.tip is not None), None)
    return pipette is None or tip is None or pipette.can_use_tip(tip)

  def tip_sizes(self, comp: Component) -> str | None:
    pipette = self._mounted(comp)
    sizes = _TIP_SIZES.get(pipette.maximum_volume) if pipette is not None else None
    return " or ".join(f"{size:g}" for size in sorted(sizes)) + " uL" if sizes else None

  def mounted_tips(self, comp: Component) -> list[Any]:
    # After a stop, or a call whose outcome is unknown, the robot's tips are the person's to check.
    if self.device is None or self.halted or self.halt_requested or self.outcome_unknown:
      return []
    pipette = self._pipette(comp)
    if comp.channels == 1:
      return [pipette.tip] if pipette.tip is not None else []
    return list(pipette.tips)

  async def pick_up_tips(self, comp: Component, spots: list[TipSpot]) -> str | None:
    pipette = self._pipette(comp)
    await (pipette.pick_up_tip(spots[0]) if comp.channels == 1 else pipette.pick_up_tips(spots))
    self._reset_tips(comp.name)  # the robot-server readies the plunger with every new tip
    return None

  @staticmethod
  def _height(liquid_height: float | None) -> float:
    if liquid_height is None:  # never the well floor by accident
      raise LabError("liquid_height_required", "the OT-2 needs a liquid height for every aspirate, dispense and mix.",
                     "Pass liquid_height (mm above the cavity bottom), or give liquid_height_mm a default in ot2.md.")
    return liquid_height

  async def _liquid(self, op: str, comp, containers: list[Container], volumes, flow_rate,
                    liquid_height) -> None:
    pipette = self._pipette(comp)
    target = containers[0] if comp.channels == 1 else containers
    await getattr(pipette, op)(target, volumes[0], flow_rate=flow_rate, liquid_height=self._height(liquid_height))

  # -- what the tip holds besides the twin's liquid ----------------------------------------------
  # The twin counts liquid only. The adapter keeps the order of what each tip holds, from the tip opening
  # inward, because a dispense moves the plunger through any air in front of the liquid it delivers.

  def _air_held(self, comp: Component) -> float:
    return sum(volume for kind, volume in self._tip_air.get(comp.name, []) if kind == "air")

  def _air_ahead(self, comp: Component, liquid: float) -> float:
    """The air the plunger pushes out before it has delivered this much liquid."""
    air, left = 0.0, liquid
    for kind, volume in self._tip_air.get(comp.name, []):
      if left <= 1e-9:
        break
      if kind == "air":
        air += volume
      else:
        left -= volume
    return air

  def _push(self, comp: Component, kind: str, volume: float) -> None:
    self._tip_air.setdefault(comp.name, []).insert(0, [kind, volume])

  def _deliver(self, comp: Component, liquid: float) -> None:
    """A dispense delivered this much liquid: it left the tip, with any air in front of it."""
    left, kept = liquid, self._tip_air.get(comp.name, [])
    while kept and left > 1e-9:
      kind, volume = kept[0]
      if kind == "air":
        kept.pop(0)
      elif volume <= left + 1e-9:
        left -= volume
        kept.pop(0)
      else:
        kept[0][1] = volume - left
        left = 0.0

  def transport_air(self, op: str, comp: Component, tips: list[Any], specialized: dict[str, Any]) -> list[float]:
    """The air each tip holds after an aspirate: what an earlier air gap left plus this call's. A dispense
    pushes it out, so it keeps no room."""
    if op != "aspirate":
      return [0.0] * len(tips)
    return [self._air_held(comp) + float(specialized.get("air_gap_ul") or 0.0)] * len(tips)

  # -- blow-out, touch tip and air gap: robot-server commands around PyLabRobot's own calls ------

  def _well(self, container: Container) -> tuple[float, Coordinate]:
    """How far the well top is above the cavity bottom (the frame liquid heights use), and the well's size."""
    deck = self.device.deck
    rise = (container.get_location_wrt(deck, "c", "c", "t").z
            - container.get_location_wrt(deck, "c", "c", "cavity_bottom").z)
    return rise, Coordinate(container.get_size_x(), container.get_size_y(), 0)

  def _above_liquid(self, what: str, containers: list[Container], at: float, added: dict[int, float]) -> None:
    """Refuse a blow-out or touch that would be under the liquid, by the twin's volume and the labware geometry
    (both estimates). added: what this call puts into each container first."""
    for container in {id(c): c for c in containers}.values():
      if container.tracker.is_disabled:
        continue
      volume = container.tracker.get_used_volume() + added.get(id(container), 0.0)
      try:
        level = container.compute_height_from_volume(volume)
      except Exception:  # noqa: BLE001 - no level for this container: not guessed
        continue
      if at + 1e-9 < level:
        raise LabError("under_liquid",
                       f"{what} would be at {at:.1f} mm above the cavity bottom of {container.name}, under its "
                       f"{level:.1f} mm of liquid (by the twin's volume and the labware geometry).",
                       "Use a higher position, or remove the liquid first.")

  def _follow_up(self, comp: Component, op: str, containers: list[Container], specialized: dict[str, Any],
                 added: dict[int, float], held_after: float | None) -> dict[str, Any] | None:
    """Check blow_out and touch_tip and work out where they go, before anything moves. None: neither is asked for.
    held_after: the liquid (uL) the tip holds when they run; a blow-out pushes all of it into the well."""
    params = comp.specialized.get(op, {})
    for name, switch in _NEEDS.items():
      if name in params and name in specialized and not specialized.get(switch) \
          and specialized[name] != params[name].default:
        raise LabError("option_without_switch", f"{name} has no effect without {switch}.",
                       f"Pass {switch}=true, or leave {name} out.")
    blow, touch = bool(specialized.get("blow_out")), bool(specialized.get("touch_tip"))
    if not (blow or touch):
      return None
    if blow and held_after and held_after > 1e-6:
      raise LabError("blow_out_empties_tip",
                     f"the tip still holds {held_after:g} uL after this {op}, and a blow-out pushes out everything "
                     "in it: the twin would be wrong.",
                     "Blow out on the call that leaves the tip empty, or dispense the rest first.")
    rise, size = self._well(containers[0])
    pipette = self._mounted(comp)
    flow_param = comp.specialized.get("dispense", {}).get("blow_out_flow_rate_ul_s")
    plan: dict[str, Any] = {}

    def spot(what: str, up: float, x: float = 0.0, y: float = 0.0) -> Coordinate:
      at = rise + up
      if at < 0:
        raise LabError("exceeds_container", f"{what} at {up:g} mm from the well top is {-at:.1f} mm below the cavity bottom.",
                       "Use a position inside the well.")
      self._above_liquid(what, containers, at, added)
      return pipette._liquid_targets(containers, Coordinate(x, y, 0), at)[1]

    if blow:
      up = float(specialized.get("blow_out_height_mm", 0.0))
      rate = specialized.get("blow_out_flow_rate_ul_s", flow_param.default if flow_param else None)
      if rate is None:
        raise LabError("blow_out_flow_rate_required", "no blow-out flow rate is known for this pipette.",
                       "Pass blow_out_flow_rate_ul_s.")
      plan["blow"] = {"at": spot("the blow-out", up), "rate": float(rate), "up": up}
    if touch:
      drop = float(specialized.get("touch_tip_v_offset_mm", -1.0))
      fraction = float(specialized.get("touch_tip_radius", 1.0))
      rx, ry = size.x / 2 * fraction, size.y / 2 * fraction
      plan["touch"] = {"drop": drop, "radius": fraction, "path": [
        spot("the touch", drop, x, y) for x, y in ((0, 0), (rx, 0), (-rx, 0), (0, 0), (0, ry), (0, -ry), (0, 0))]}
    return plan

  async def _follow(self, comp: Component, plan: dict[str, Any]) -> list[str]:
    """Blow out at the well top, then touch the tip to the four walls. One robot-server command each; the
    moves between them are PyLabRobot's own (moveToCoordinates), under its operation lock."""
    device, pipette, notes = self.device, self._mounted(comp), []
    async with device._operation_lock:
      pipette._require_active()
      if "blow" in plan:
        blow = plan["blow"]
        self._unprimed.add(comp.name)  # before the command: from here on the plunger may be past its bottom
        self._tip_air.pop(comp.name, None)  # a blow-out empties the tip, air included
        await pipette._move_to(blow["at"], minimum_z_height=device.traversal_height)
        await device._run.execute("blowOutInPlace", {"pipetteId": pipette.pipette_id, "flowRate": blow["rate"]})
        notes.append(f"blew out at {blow['rate']:g} uL/s, {blow['up']:g} mm from the well top (blowOutInPlace)")
      if "touch" in plan:
        touch = plan["touch"]
        first, *edges = touch["path"]
        await pipette._move_to(first, minimum_z_height=None if "blow" in plan else device.traversal_height,
                               force_direct="blow" in plan)
        for waypoint in edges:
          await pipette._move_to(waypoint, speed=TOUCH_TIP_SPEED_MM_S, force_direct=True)
        notes.append(f"touched the tip to the four sides of the well {-touch['drop']:g} mm below its top at "
                     f"{touch['radius']:g} of its radius ({TOUCH_TIP_SPEED_MM_S:g} mm/s, moveToCoordinates)")
      await pipette._retract_to_traversal_height()
    return notes

  async def _prime(self, comp: Component, containers: list[Container]) -> str | None:
    """After a blow-out the robot-server refuses any in-place aspirate until the plunger is prepared again, which
    it does above the well (its own aspirate does the same). The tip is empty then: a blow-out needs that."""
    if comp.name not in self._unprimed:
      return None
    device, pipette = self.device, self._mounted(comp)
    async with device._operation_lock:
      pipette._require_active()
      rise, _ = self._well(containers[0])
      at = pipette._liquid_targets(containers, Coordinate.zero(), rise)[1]
      await pipette._move_to(at, minimum_z_height=device.traversal_height)
      await device._run.execute("prepareToAspirate", {"pipetteId": pipette.pipette_id})
      self._unprimed.discard(comp.name)
      await pipette._retract_to_traversal_height()
    return "prepared the plunger above the well after the blow-out (prepareToAspirate)"

  async def _air_gap(self, comp: Component, containers: list[Container], volume: float, flow_rate: float | None) -> str:
    """Rise to the well top and draw air: airGapInPlace, or aspirateInPlace on a robot-server without it."""
    device, pipette = self.device, self._mounted(comp)
    rate = pipette._spec.default_aspiration_flow_rate if flow_rate is None else flow_rate
    async with device._operation_lock:
      pipette._require_active()
      rise, _ = self._well(containers[0])
      at = pipette._liquid_targets(containers, Coordinate.zero(), rise)[1]
      await pipette._move_to(at, minimum_z_height=device.traversal_height)
      params = {"pipetteId": pipette.pipette_id, "volume": volume, "flowRate": rate}
      command = "airGapInPlace"
      try:
        await device._run.execute(command, params)
      except HTTPError as exc:
        if exc.status != 422:  # 422: this robot-server does not know the command; nothing was queued
          raise
        command = "aspirateInPlace"
        await device._run.execute(command, params)
      self._push(comp, "air", volume)
      await pipette._retract_to_traversal_height()
    return f"drew {volume:g} uL of air at the well top ({command})"

  async def aspirate(self, comp, containers, volumes, flow_rate, liquid_height, specialized):
    air = float(specialized.get("air_gap_ul") or 0.0)
    notes = [await self._prime(comp, containers)]
    await self._liquid("aspirate", comp, containers, volumes, flow_rate, liquid_height)
    self._push(comp, "liquid", volumes[0])
    if air:
      notes.append(await self._air_gap(comp, containers, air, flow_rate))
    return "; ".join(n for n in notes if n) or None

  async def dispense(self, comp, containers, volumes, flow_rate, liquid_height, specialized):
    pipette = self._pipette(comp)
    volume = volumes[0]
    held = [t.tracker.get_used_volume() for t in self.mounted_tips(comp) if not t.tracker.is_disabled]
    plan = self._follow_up(comp, "dispense", containers, specialized, self._volume_per(containers, volumes),
                           max(held, default=0.0) - volume if held else None)
    air = self._air_ahead(comp, volume)
    notes = []
    if air:  # PyLabRobot moves the plunger by the liquid alone: this one moves it through the air as well
      await self._dispense_through_air(comp, containers, volume, air, flow_rate, self._height(liquid_height))
      notes.append(f"pushed out {air:g} uL of air in front of the liquid (the pipette moved {volume + air:g} uL)")
    else:
      await self._liquid("dispense", comp, containers, volumes, flow_rate, liquid_height)
    self._deliver(comp, volume)
    if plan:
      notes += await self._follow(comp, plan)
    return "; ".join(notes) or None

  @staticmethod
  def _volume_per(containers: list[Container], volumes: list[float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for container, volume in zip(containers, volumes):
      out[id(container)] = out.get(id(container), 0.0) + volume
    return out

  async def _dispense_through_air(self, comp: Component, containers: list[Container], volume: float, air: float,
                                  flow_rate: float | None, liquid_height: float) -> None:
    """PyLabRobot's dispense with the plunger moved volume + air: the same steps and bookkeeping."""
    device, pipette = self.device, self._mounted(comp)
    async with device._operation_lock:
      pipette._require_active()
      tips = pipette._require_tips()
      volume = pipette._validate_volume(volume)
      rate = pipette._spec.default_dispense_flow_rate if flow_rate is None else float(flow_rate)
      trackers, location = pipette._liquid_targets(containers, Coordinate.zero(), liquid_height)
      with track_liquid_transfer(tuple(tip.tracker for tip in tips), trackers, volume):
        await pipette._move_to(location, minimum_z_height=device.traversal_height)
        await device._run.dispense_in_place(pipette.pipette_id, volume + air, rate)
      await pipette._retract_to_traversal_height()

  async def mix(self, comp, containers, volume, repetitions, flow_rate, liquid_height, specialized):
    pipette = self._pipette(comp)
    target = containers[0] if comp.channels == 1 else containers
    held = max((t.tracker.get_used_volume() for t in self.mounted_tips(comp) if not t.tracker.is_disabled),
               default=0.0)
    tip = self.mounted_tips(comp)[0]
    air = self._air_held(comp)
    if air and held + air + volume > tip.maximal_volume + 1e-9:  # the air a gap left takes room
      raise LabError("exceeds_tip", f"{volume:g} uL on top of the {held:g} uL of liquid and {air:g} uL of air the "
                     f"tips hold exceeds the mounted {tip.maximal_volume:g} uL tips.",
                     "Use a smaller mix volume, or dispense first.")
    plan = self._follow_up(comp, "mix", containers, specialized, {}, held or None)
    notes = [await self._prime(comp, containers)]
    await pipette.mix(target, volume, repetitions, aspiration_flow_rate=flow_rate,
                      dispense_flow_rate=flow_rate, liquid_height=self._height(liquid_height))
    if plan:
      notes += await self._follow(comp, plan)
    return "; ".join(n for n in notes if n) or None

  async def drop_tips(self, comp: Component, mode: str) -> str | None:
    pipette = self._pipette(comp)
    single = comp.channels == 1
    if mode == "discard":
      await (pipette.discard_tip() if single else pipette.discard_tips())
    else:
      await (pipette.return_tip() if single else pipette.return_tips())
    self._reset_tips(comp.name)
    return None
