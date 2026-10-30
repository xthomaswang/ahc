"""Opentrons OT-2: driven step by step through PyLabRobot 1.0 over the robot-server HTTP API."""

from __future__ import annotations

import asyncio
import time
import urllib.error
from typing import Any

from pylabrobot.resources import Container, OTDeck, TipSpot

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


def _close_transport(device) -> None:
  """PyLabRobot's HTTP transport, closed without touching the run. Blocking: callers use a thread."""
  asyncio.run(device.io.stop())


class OT2Adapter(OpentronsAdapter):
  """An OT-2 driven step by step through PyLabRobot 1.0 over the robot-server HTTP API."""

  _EXPECTED = {"right": ("p300_multi_gen2", 8), "left": ("p20_single_gen2", 1)}

  async def start(self) -> None:
    await super().start()
    self.device = None
    self._setting_up = None  # the OT2 whose setup() runs now: the emergency stop reaches its run too
    self._sent = 0  # run commands this adapter submitted, over all its runs
    self._recent: list[dict[str, Any]] = []  # the last RECORD_KEPT of them: run, command type, id

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
      raise LabError("mount_mismatch", "; ".join(mounts["problems"]) + "; nothing was moved.",
                     "Fix the description file or the robot before running.")
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
    for mount, (name, channels) in self._EXPECTED.items():
      pipette = getattr(device, f"{mount}_pipette")
      if pipette is None or pipette.name != name:  # changed since the check above
        try:
          await device.stop()
        except Exception:  # noqa: BLE001 - the mismatch is what the person must hear about
          pass
        raise LabError("mount_mismatch",
                       f"{mount} mount has {getattr(pipette, 'name', None)!r}, the description file expects {name!r}.",
                       "Fix the description file or the robot before running.")
    self.device = device
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
    """By PyLabRobot's own rule for that pipette (the P20 takes 10 and 20 uL tips, the P300 200 and 300 uL)."""
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

  async def aspirate(self, comp, containers, volumes, flow_rate, liquid_height, specialized):
    await self._liquid("aspirate", comp, containers, volumes, flow_rate, liquid_height)
    return None

  async def dispense(self, comp, containers, volumes, flow_rate, liquid_height, specialized):
    await self._liquid("dispense", comp, containers, volumes, flow_rate, liquid_height)
    return None

  async def mix(self, comp, containers, volume, repetitions, flow_rate, liquid_height, specialized):
    pipette = self._pipette(comp)
    target = containers[0] if comp.channels == 1 else containers
    await pipette.mix(target, volume, repetitions, aspiration_flow_rate=flow_rate,
                      dispense_flow_rate=flow_rate, liquid_height=self._height(liquid_height))
    return None

  async def drop_tips(self, comp: Component, mode: str) -> str | None:
    pipette = self._pipette(comp)
    single = comp.channels == 1
    if mode == "discard":
      await (pipette.discard_tip() if single else pipette.discard_tips())
    else:
      await (pipette.return_tip() if single else pipette.return_tips())
    return None
