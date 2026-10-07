"""The capability layer: general liquid-handling operations addressed to a device component.

Every call names a component (`starlet.pip`, `ot2.right`) and passes basic parameters with the same
meaning on every device. Specialized parameters are accepted only if the component's description
file declares them. Each call, refused or not, lands in the run log with what actually took effect.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pylabrobot.resources import Container, Plate

from ahc.devices.base import Adapter
from ahc.devices.spec import Component, DeviceSpec
from ahc.core.errors import LabError
from ahc.records import RunLog

Number = float | int


class LiquidHandling:
  def __init__(self, spec: DeviceSpec, adapter: Adapter, runlog: RunLog):
    self.spec = spec
    self.adapter = adapter
    self.runlog = runlog

  # -- validation -----------------------------------------------------------------------------

  def component(self, device: str) -> Component:
    comp = self.spec.component(device)
    if "liquid_handling" not in comp.capabilities:
      able = [f"{self.spec.prefix}.{c.name}" for c in self.spec.components.values()
              if "liquid_handling" in c.capabilities]
      raise LabError("not_supported", f"{device} has no liquid_handling capability.",
                     f"Components that do: {', '.join(able)}." + (f" {comp.note}" if comp.note else ""))
    return comp

  def specialized(self, comp: Component, op: str, given: dict[str, Any] | None) -> dict[str, Any]:
    given = given or {}
    allowed = comp.specialized.get(op, {})
    unknown = sorted(set(given) - set(allowed))
    if unknown:
      raise LabError("unknown_param",
                     f"{op} on {self.spec.prefix}.{comp.name} has no parameter(s) {unknown}.",
                     f"Specialized parameters for {op}: {sorted(allowed) or 'none, basic parameters only'}.")
    out = {key: allowed[key].check(value) for key, value in given.items()}
    for key, param in allowed.items():
      if key not in out and param.tightened:
        out[key] = self._task_default(param, f"specialized.{key}")
    return out

  @staticmethod
  def _task_default(param, name: str) -> Any:
    """A parameter with a task limit that the call left out: never fall to an unknown device default."""
    if param.covers_default():
      return param.check(param.default)
    raise LabError("limit_requires_value",
                   f"{name} has a task limit {param.min}..{param.max}; leaving it out would let the device "
                   "choose a value outside it.",
                   f"Pass {name} explicitly, between {param.min} and {param.max}.")

  def _tips(self, comp: Component, address: str) -> list[Any]:
    tips = self.adapter.mounted_tips(comp)
    if not tips:
      raise LabError("no_tips", f"{address} carries no tips.", "Call pick_up_tips first.")
    return tips

  def _containers(self, comp: Component, address: str, targets: list[str], n: int) -> list[Container]:
    containers = self.adapter.require_layout().resolve_targets(targets)
    if len(containers) == 1 and n > 1:
      if not comp.shared_container:
        raise LabError("distinct_wells_required",
                       f"{address} needs one distinct well per channel; {targets[0]!r} is one container for {n} channels.",
                       self.spec.layout.get("note") or "Name one well per channel, e.g. plate:A1:H1.")
      containers = containers * n
    if len(containers) != n:
      raise LabError("target_count", f"{len(containers)} targets for {n} mounted tips.",
                     "Give one target per mounted tip, or one container all channels share.")
    if not comp.shared_container and len({id(c) for c in containers}) != n:
      raise LabError("distinct_wells_required", f"{address} needs a different well for each channel.")
    return containers

  def _volumes(self, comp: Component, address: str, volumes: Number | list[Number], n: int,
               tips: list[Any]) -> list[float]:
    values = [volumes] * n if isinstance(volumes, (int, float)) else list(volumes)
    if len(values) != n:
      raise LabError("volume_count", f"{len(values)} volumes for {n} targets.",
                     "Give one volume per target, or one number for all.")
    values = [comp.limits["volume_ul"].check(v) for v in values]
    if comp.equal_volumes and len(set(values)) > 1:
      raise LabError("equal_volumes_required",
                     f"{address} moves one volume on every channel; got {sorted(set(values))}.",
                     "Use one volume for all targets, or a component with independent channels.")
    tip_max = min(t.maximal_volume for t in tips)
    if max(values) > tip_max:
      raise LabError("exceeds_tip", f"{max(values):g} uL exceeds the mounted {tip_max:g} uL tips.",
                     "Split the transfer or mount larger tips.")
    return values

  def _height(self, comp: Component, height: Number | None, containers: list[Container]) -> float | None:
    if height is None:
      if not comp.limits["liquid_height_mm"].tightened:
        return None
      height = self._task_default(comp.limits["liquid_height_mm"], "liquid_height")
    value = comp.limits["liquid_height_mm"].check(height)
    depth = min(c.get_size_z() for c in containers)
    if value > depth:
      raise LabError("exceeds_container", f"liquid_height {value:g} mm is above the {depth:.1f} mm cavity.",
                     f"Use at most {depth:.1f} mm.")
    return value

  def _flow(self, comp: Component, flow_rate: Number | None) -> float | None:
    param = comp.limits["flow_rate_ul_s"]
    if flow_rate is None:
      return self._task_default(param, "flow_rate") if param.tightened else None
    return param.check(flow_rate)

  @staticmethod
  def _liquid(op: str, containers: list[Container], volumes: list[float]) -> None:
    """Refuse drawing more than a container holds, or dispensing more than it fits.

    PyLabRobot's STAR driver draws air past an empty container with only a warning, so the twin's
    volumes are checked here, summed per container when several channels share one.
    """
    need: dict[int, float] = {}
    for container, volume in zip(containers, volumes):
      need[id(container)] = need.get(id(container), 0.0) + volume
    for container in {id(c): c for c in containers}.values():
      tracker = container.tracker
      if tracker.is_disabled:
        continue
      if op == "aspirate" and need[id(container)] > tracker.get_used_volume() + 1e-9:
        raise LabError("insufficient_liquid",
                       f"{container.name} holds {tracker.get_used_volume():g} uL; {need[id(container)]:g} uL requested.",
                       "Draw less, or add liquid to the layout.")
      if op == "dispense" and need[id(container)] > tracker.get_free_volume() + 1e-9:
        raise LabError("overfill",
                       f"{container.name} has room for {tracker.get_free_volume():g} uL; {need[id(container)]:g} uL requested.",
                       "Dispense less or into another container.")

  # -- execution ------------------------------------------------------------------------------

  def _twin_volumes(self) -> dict[str, float]:
    layout = self.adapter.layout
    if layout is None:
      return {}
    out: dict[str, float] = {}
    for res in layout.labware.values():
      items = res.get_all_items() if isinstance(res, Plate) else [res] if isinstance(res, Container) else []
      for c in items:
        if not c.tracker.is_disabled:
          out[c.name] = c.tracker.get_used_volume()
    return out

  def _refusal(self, entry: dict[str, Any], exc: LabError, before: dict[str, float]) -> LabError:
    """Log a refusal. If liquid had already moved (a refusal half-way), say how much, so the run log and
    the agent agree with the device instead of reading the refusal as "nothing happened"."""
    after = self._twin_volumes()
    moved = {k: round(after[k] - v, 3) for k, v in before.items() if k in after and abs(after[k] - v) > 1e-6}
    record = {**entry, "status": "refused", "error": exc.code, "message": str(exc)}
    if moved:
      record["partial"] = moved
      shown = ", ".join(f"{k} {d:+g} uL" for k, d in list(moved.items())[:8]) + (" ..." if len(moved) > 8 else "")
      exc = LabError(exc.code, f"{exc.message} Partly done before the refusal: {shown}; the tips hold the difference.",
                     (exc.hint or "") + " Check get_state before going on.")
    self.runlog.record(record)
    return exc

  async def _run(self, op: str, address: str, basic: dict[str, Any], specialized: dict[str, Any] | None,
                 body: Callable[[], Awaitable[tuple[dict[str, Any], str | None, bool]]]) -> dict[str, Any]:
    """Run one operation and log it, refused or not."""
    before = self.adapter.command_count()
    volumes_before = self._twin_volumes()
    entry: dict[str, Any] = {"op": op, "device": address, "basic": basic, "specialized": specialized or {}}
    try:
      effective, note, translated = await body()
    except LabError as exc:
      error = self._refusal(entry, exc, volumes_before)
      if error is exc:
        raise
      raise error from exc
    except Exception as exc:  # noqa: BLE001 - the device model's own refusals (no tip, too little liquid)
      error = LabError("device_refused", f"{type(exc).__name__}: {exc}",
                       "The device model refused it; nothing past the refusal was sent.")
      raise self._refusal(entry, error, volumes_before) from exc
    commands = self.adapter.command_count() - before
    self.runlog.record({**entry, "status": "ok", "effective": effective, "note": note,
                        "translated": translated, "commands": commands})
    return {"ok": True, "op": op, "device": address, "effective": effective, "note": note,
            "translated": translated, "commands_sent": commands, "simulated": self.adapter.simulated}

  def _defaults(self, comp: Component, op: str, given: dict[str, Any]) -> list[str]:
    return sorted(set(comp.specialized.get(op, {})) - set(given))

  async def pick_up_tips(self, device: str, tips: list[str] | None) -> dict[str, Any]:
    comp = self.component(device)

    async def body():
      if self.adapter.mounted_tips(comp):
        raise LabError("tips_already_mounted", f"{device} already carries tips.", "Call drop_tips first.")
      layout = self.adapter.require_layout()
      spots = layout.resolve_tips(tips) if tips else layout.next_tips(comp.channels)
      note = await self.adapter.pick_up_tips(comp, spots)
      return {"tips": [s.name for s in spots]}, note, False

    return await self._run("pick_up_tips", device, {"tips": tips or "next fresh"}, None, body)

  async def _move(self, op: str, device: str, targets: list[str], volumes: Number | list[Number],
                  flow_rate: Number | None, liquid_height: Number | None,
                  specialized: dict[str, Any] | None) -> dict[str, Any]:
    comp = self.component(device)

    async def body():
      self.adapter.require_layout()
      extra = self.specialized(comp, op, specialized)
      tips = self._tips(comp, device)
      containers = self._containers(comp, device, targets, len(tips))
      vols = self._volumes(comp, device, volumes, len(tips), tips)
      height = self._height(comp, liquid_height, containers)
      flow = self._flow(comp, flow_rate)
      self._liquid(op, containers, vols)
      fn = self.adapter.aspirate if op == "aspirate" else self.adapter.dispense
      note = await fn(comp, containers, vols, flow, height, extra)
      effective = {"containers": [c.name for c in containers], "volumes_ul": vols,
                   "flow_rate_ul_s": flow if flow is not None else "device default",
                   "liquid_height_mm": height if height is not None else "device default",
                   "specialized": extra, "left_to_device": self._defaults(comp, op, extra)}
      return effective, note, op in comp.translations

    basic = {"targets": targets, "volumes": volumes, "flow_rate": flow_rate, "liquid_height": liquid_height}
    return await self._run(op, device, basic, specialized, body)

  async def aspirate(self, device, targets, volumes, flow_rate=None, liquid_height=None, specialized=None):
    return await self._move("aspirate", device, targets, volumes, flow_rate, liquid_height, specialized)

  async def dispense(self, device, targets, volumes, flow_rate=None, liquid_height=None, specialized=None):
    return await self._move("dispense", device, targets, volumes, flow_rate, liquid_height, specialized)

  async def mix(self, device: str, targets: list[str], volume: Number, repetitions: int,
                flow_rate: Number | None = None, liquid_height: Number | None = None,
                specialized: dict[str, Any] | None = None) -> dict[str, Any]:
    comp = self.component(device)

    async def body():
      self.adapter.require_layout()
      extra = self.specialized(comp, "mix", specialized)
      tips = self._tips(comp, device)
      containers = self._containers(comp, device, targets, len(tips))
      vol = self._volumes(comp, device, volume, len(tips), tips)[0]
      reps = comp.limits["mix_repetitions"].check(repetitions)
      height = self._height(comp, liquid_height, containers)
      flow = self._flow(comp, flow_rate)
      self._liquid("aspirate", containers, [vol] * len(containers))  # each cycle draws this much
      note = await self.adapter.mix(comp, containers, vol, int(reps), flow, height, extra)
      effective = {"containers": [c.name for c in containers], "volume_ul": vol, "repetitions": int(reps),
                   "flow_rate_ul_s": flow if flow is not None else "device default",
                   "liquid_height_mm": height if height is not None else "device default"}
      return effective, note, "mix" in comp.translations

    basic = {"targets": targets, "volume": volume, "repetitions": repetitions,
             "flow_rate": flow_rate, "liquid_height": liquid_height}
    return await self._run("mix", device, basic, specialized, body)

  async def drop_tips(self, device: str, mode: str = "discard") -> dict[str, Any]:
    comp = self.component(device)

    async def body():
      if mode not in ("discard", "return"):
        raise LabError("bad_mode", f"mode {mode!r} is not discard or return.")
      self._tips(comp, device)
      note = await self.adapter.drop_tips(comp, mode)
      return {"mode": mode}, note, False

    return await self._run("drop_tips", device, {"mode": mode}, None, body)
