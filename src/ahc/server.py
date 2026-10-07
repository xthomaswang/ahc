"""The MCP server: one task workspace, one device, a small fixed tool set, limits next to every parameter.

The server runs in a task folder (or --workspace / AHC_WORKSPACE) and writes nothing until a tool
needs to; the task's config, layouts and run logs then live under <folder>/.ahc/. Which device it
drives comes from that config, never from the client: `--backend sim` can only force simulation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated, Any, Awaitable, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from pylabrobot.resources import Container, Plate, TipRack, set_tip_tracking, set_volume_tracking

from ahc import __version__
from ahc.core.capability import LiquidHandling
from ahc.core.errors import LabError
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.devices.opentrons.sim import robot_server as ot2_sim
from ahc.devices.spec import LIQUID_HANDLING_OPS, DeviceSpec, known_models, load_device
from ahc.plan import MAX_STEPS, PlanProgress
from ahc.records import RunLog
from ahc.verification import CHECKS, Gate, Verification
from ahc.workspace import (Config, Workspace, apply_limits, parse_config, parse_devices, resolve_workspace,
                           simulation_config, template_text)
from ahc.workspace.config import HEADER
from ahc.workspace import estop
from ahc.workspace.limits import keeps_limits

ADAPTERS = {"hamilton.starlet": STARletAdapter, "opentrons.ot2": OT2Adapter}
# Models whose simulation runs as a separate per-task process (ahc-sim); the STARlet's is in-process.
SIMULATORS = {"opentrons.ot2": ot2_sim}
DEFAULT_SIM_MODEL = "hamilton.starlet"

INSTRUCTIONS = """\
Lab hardware MCP (AHC): call lab_overview first. It shows this task's workspace (the folder the server runs in), its device config and the next step.
Config: when the server forces simulation, a simulation config is created on first use. Otherwise list the available models to the person, let them choose, write the choice with configure_devices, and ask the person to confirm it with confirm_config; nothing moves before that. Never call confirm_config or record_verdict on your own.
Flow: lab_overview -> find_layout_references -> report_decision(plan=[...]) -> load_layout -> verify(check='deck_matches_layout') -> pick_up_tips / aspirate / dispense / mix / drop_tips with basic parameters -> get_params(device, op) only when you need device-specific behaviour -> verify -> report_decision(plan_done=true).
Plan first: before load_layout or any action, report your plan as short concrete steps with report_decision (refused otherwise: plan_required). Every load_layout, verify and action call then names its step with plan_step; the person's dashboard ticks a step once you move on to a later one. When the last step is done, report the result with report_decision(plan_done=true).
After every load_layout nothing moves until the deck check passes: the simulator judges it in simulation, the person on site on real hardware. A layout that follows no reference in this task folder (the built-in example, or your own) needs the person in simulation too: show them the labware, positions and liquids, and wait for their verdict.
Address a component as <device id>.<component>, e.g. starlet.pip or ot2.right.
A target is 'labware', 'labware:A1', 'labware:A1:H1', or an alias defined in load_layout. Volumes are per target; one number is broadcast.
Each parameter's limits come with its description, and every call is checked server-side before anything moves. Results marked simulated are not measurements.
Tell the person watching what you decide with report_decision: the plan, a changed plan, a refusal that changes your course, and waiting for the person (waiting_for='person'). One line each; not for every call.
If a call is refused with emergency_stop, stop at once: do not retry or work around it, never release it yourself, tell the person and wait (report_decision with waiting_for='person')."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MOTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
CONFIGURE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
NOTE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
GATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
PERSON_ONLY = ToolAnnotations(title="Person on site only - never auto-approve", readOnlyHint=False,
                              destructiveHint=True, idempotentHint=False, openWorldHint=False)

BASIC_PARAMS = {
  "pick_up_tips": {"tips": {"type": "list[str] | null", "doc": "Tip spots such as tips:A1:H1; omitted = next fresh column (or tip for one channel)."}},
  "aspirate": {"targets": {"type": "list[str]", "doc": "One target per mounted tip, or one container all channels share."},
               "volumes": {"type": "number | list[number]", "limit": "volume_ul"},
               "flow_rate": {"type": "number | null", "limit": "flow_rate_ul_s"},
               "liquid_height": {"type": "number | null", "limit": "liquid_height_mm"}},
  "mix": {"targets": {"type": "list[str]"}, "volume": {"type": "number", "limit": "volume_ul"},
          "repetitions": {"type": "integer", "limit": "mix_repetitions"},
          "flow_rate": {"type": "number | null", "limit": "flow_rate_ul_s"},
          "liquid_height": {"type": "number | null", "limit": "liquid_height_mm"}},
  "drop_tips": {"mode": {"type": "'discard' | 'return'", "doc": "Discard into the trash, or return to the spots they came from."}},
}
BASIC_PARAMS["dispense"] = BASIC_PARAMS["aspirate"]

LAYOUT_EXAMPLES = {
  "tracks": {"carriers": [
    {"name": "tip_car", "type": "TIP_CAR_480_A00", "track": 1,
     "sites": {"0": {"name": "tips", "type": "hamilton_96_tiprack_300uL_filter"}}},
    {"name": "plate_car", "type": "PLT_CAR_L5AC_A00", "track": 7,
     "sites": {"0": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}}},
    {"name": "trough_car", "type": "Trough_CAR_4R200_A00", "track": 13,
     "sites": {"0": {"name": "diluent", "type": "Hamilton_1_trough_200ml_Vb"}}}],
    "liquids": {"diluent": 100000}, "aliases": {}},
  "slots": {"slots": {"1": {"name": "tips", "type": "opentrons_96_tiprack_300ul"},
                      "2": {"name": "reagents", "type": "cor_96_wellplate_2mL_Vb"},
                      "3": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"}},
            "liquids": {"reagents:A1:H1": 1800}, "aliases": {"diluent": "reagents:A1:H1"}},
}


# Tool parameter types (module level so the SDK can resolve them).
Device = Annotated[str, Field(description="Component address, e.g. starlet.pip or ot2.right.")]
Targets = Annotated[list[str], Field(description="'labware', 'labware:A1', 'labware:A1:H1' or an alias; one per mounted tip, or one shared container.")]
Volumes = Annotated[float | list[float], Field(description="uL per target; one number is broadcast. Limits: describe_device / get_params.")]
FlowRate = Annotated[float | None, Field(description="uL/s; omitted = device default (unless a task limit applies).")]
Height = Annotated[float | None, Field(description="mm above the cavity bottom; omitted = device default (unless a task limit applies).")]
Specialized = Annotated[dict[str, Any] | None, Field(description="Device-specific parameters from get_params(device, op); unknown keys are refused.")]
DeviceList = Annotated[list[dict[str, Any]], Field(description="The person's choice, e.g. [{'id': 'starlet', 'model': 'hamilton.starlet', 'backend': 'sim'}]; one device for now.")]
Plan = Annotated[list[str] | None, Field(description="Your plan as short concrete steps; replaces the previous plan. Omit to keep it.")]
PlanStep = Annotated[int, Field(description="The step of your reported plan this call belongs to (1-based); report the plan first with report_decision.")]
TaskLimits = Annotated[dict[str, Any] | None, Field(description="Optional, tighten only: {'starlet.pip': {'volume_ul': {'max': 200}}}.")]


class Lab:
  """The task workspace, its config, and the one device that config names (built on first use)."""

  def __init__(self, workspace, forced_sim: bool, default_model: str | None, options: dict[str, Any],
               view: bool):
    self.ws = Workspace(workspace)
    self.forced_sim = forced_sim
    self.default_model = default_model  # only used when a simulation config is generated
    self.options = {k: v for k, v in (options or {}).items() if v is not None}
    # PyLabRobot drivers take one command at a time; parallel tool calls queue here.
    self.lock = asyncio.Lock()
    self.config: Config | None = None
    self.on_disk = False  # False: the forced-simulation config, held in memory until a tool writes
    self.problem: LabError | None = None  # why the config on disk cannot be used
    self.spec: DeviceSpec | None = None
    self.adapter = None
    self.lh: LiquidHandling | None = None
    self.verification: Verification | None = None
    self.gate: Gate | None = None  # the motion gate of the loaded layout
    self.runlog: RunLog | None = None
    self.estop: dict[str, Any] | None = None  # the engaged emergency stop, held until a release record
    self._estop_mtime: float | None = None
    self.current_action: asyncio.Task | None = None  # what the emergency stop interrupts
    self.plan = PlanProgress()  # the agent's plan; fed by the run log entries this server records
    self.step_context: int | None = None  # the plan step of the call running now
    self._bound: str | None = None
    self._audited: str | None = None
    self.view = None
    if view:
      from ahc.viz.viewer import LiveView  # optional: pulls in the 3D viewer's web servers
      self.view = LiveView("AHC")

  async def run(self, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    async with self.lock:
      return await fn()

  # -- emergency stop -------------------------------------------------------------------------

  def poll_estop(self) -> None:
    """Read .ahc/estop.json when it changed: latch an engage, clear only on its release record."""
    try:
      mtime = estop.path(self.ws).stat().st_mtime
    except OSError:
      if self.estop is not None:  # deleted while engaged: still stopped, so put the record back
        estop._write(self.ws, self.estop)
        self.record({"type": "audit", "event": "estop_file_removed",
                     "detail": "the emergency stop file was deleted while engaged; the server wrote it back"})
      return
    if mtime == self._estop_mtime:
      return
    self._estop_mtime = mtime
    data = estop.read(self.ws)
    if not data:
      return
    if data.get("engaged") and (self.estop is None or self.estop.get("id") != data.get("id")):
      self.estop = data
      interrupted = self.current_action is not None and not self.current_action.done()
      if interrupted:
        self.current_action.cancel()
      self.record({"type": "estop", "state": "engaged", "by": data.get("by"), "at": data.get("at"),
                   "reason": data.get("reason"), "interrupted": interrupted})
    elif not data.get("engaged") and self.estop is not None and self.estop.get("id") == data.get("id"):
      self.estop = None
      self.record({"type": "estop", "state": "released", "by": data.get("released_by"), "at": data.get("released_at")})
      if self.gate is not None:  # the device stopped mid-run: the deck has to be checked again
        self.gate = Gate(layout=self.gate.layout, needs_person=self.gate.needs_person,
                         pending_layout=self.gate.pending_layout)
        self.gate_changed("emergency stop released: check the deck again")

  async def watch_estop(self) -> None:
    """Runs for the server's life; a stat every 0.2 s, parsing only when the file changes."""
    while True:
      self.poll_estop()
      await asyncio.sleep(0.2)

  def require_not_stopped(self) -> None:
    self.poll_estop()
    if self.estop is not None:
      raise LabError("emergency_stop",
                     f"the emergency stop was pressed at {(self.estop.get('at') or '').replace('T', ' ')} "
                     f"({self.estop.get('by')}); nothing runs until the person releases it.",
                     "Stop now and tell the person. Do not retry, work around it or release it yourself.")

  async def shutdown(self) -> None:
    async with self.lock:
      await self._unbind()
      if self.view is not None:
        await self.view.stop()

  # -- workspace config -----------------------------------------------------------------------

  async def refresh(self, write: bool) -> None:
    """Read the workspace config again, create it on the first write, and (re)build the device."""
    text = self.ws.read_config_text()
    if text is None and write:
      self._create_config()
      text = self.ws.read_config_text()
    try:
      if text is None and not self.forced_sim:
        self.config, self.problem, self.on_disk = None, None, False
        await self._unbind()
        return
      config = self._simulation_config() if text is None else parse_config(text)
      self._check_device_flag(config)
    except LabError as exc:
      self.config, self.problem = None, exc
      await self._unbind()
      return
    self.config, self.problem, self.on_disk = config, None, text is not None
    if config.confirmation and config.confirmation.get("digest") != config.digest():
      self._audit_outside_edit(config)
    if config.status(self.forced_sim)[0] != "confirmed":
      await self._unbind()
      return
    try:
      await self._bind(config)
    except LabError as exc:  # e.g. task limits that would loosen the description file's
      self.problem = exc
      await self._unbind()

  def _simulation_config(self) -> Config:
    model = self.default_model or DEFAULT_SIM_MODEL
    if model not in known_models():
      raise LabError("unknown_model", f"no description file for {model!r}.", f"Models: {', '.join(known_models())}.")
    return simulation_config(model)

  def _create_config(self) -> None:
    self.ws.ensure()
    if self.forced_sim:
      config = self._simulation_config()
      for sim in SIMULATORS.values():  # a simulator started before the config existed
        config.sim.update(sim.base_versions(self.ws))
      self.ws.write_config(config.data(), HEADER)
    else:
      self.ws.config_path.write_text(template_text())

  def _check_device_flag(self, config: Config) -> None:
    if self.default_model and config.devices and config.devices[0].model != self.default_model:
      raise LabError("device_conflict",
                     f"this folder's config names {config.devices[0].model}, but the server was started for "
                     f"{self.default_model} (--device / AHC_DEVICE).",
                     "Drop --device, use another folder, or change the config with configure_devices.")

  def _audit_outside_edit(self, config: Config) -> None:
    digest = config.digest()
    if self._audited != digest:
      self._audited = digest
      self.record({"type": "audit", "event": "config_edited_outside_server", "config_digest": digest,
                   "detail": "devices or limits differ from what was confirmed; the config is pending again"})

  async def _bind(self, config: Config) -> None:
    entry = config.devices[0]
    backend = "sim" if self.forced_sim else entry.backend
    key = f"{config.digest()}|{backend}"
    if self._bound == key:
      return
    spec = apply_limits(replace(load_device(entry.model), id=entry.id), config.limits)
    options = {**entry.options, **self.options}
    sim = SIMULATORS.get(spec.model) if backend == "sim" else None
    if sim is not None:  # this task's simulator instance: its port, and why it might not answer
      explicit = "port" in options  # a port the person set; otherwise only this task's own instance
      options = {**sim.workspace_options(self.ws), **options,
                 "diagnose_unreachable": lambda host, port, exc: sim.diagnose(self.ws, host, port, exc)}
      if not explicit:
        options["check_endpoint"] = lambda host, port: sim.check_endpoint(self.ws, host, port)
    adapter = ADAPTERS[spec.model](spec, backend, options)
    await self._unbind()
    set_tip_tracking(True)
    set_volume_tracking(True)
    await adapter.start()
    if self.runlog is not None and self.runlog.device != spec.model:
      self.runlog = None  # one device per run log
    self.spec, self.adapter, self._bound = spec, adapter, key
    self.lh = LiquidHandling(spec, adapter, self)
    self.verification = Verification(spec, adapter, self)

  async def _unbind(self) -> None:
    if self.adapter is not None:
      await self.adapter.stop()
    self.spec = self.adapter = self.lh = self.verification = self.gate = None
    self._bound = None

  def config_status(self) -> dict[str, Any]:
    out: dict[str, Any] = {"path": str(self.ws.config_path)}
    if self.problem is not None:
      return {**out, "status": "invalid", "error": self.problem.code, "reason": self.problem.message,
              "hint": self.problem.hint}
    if self.config is None:
      if self.forced_sim:
        reason = f"none yet; a simulation config for {self.default_model or DEFAULT_SIM_MODEL} is created on first use"
      else:
        reason = "none yet; the first write creates it from the template, pending the person's confirmation"
      return {**out, "status": "none", "reason": reason}
    status, why = self.config.status(self.forced_sim)
    if not self.on_disk:
      status, why = "none", "not written yet; this simulation config is created on first use"
    return {**out, "status": status, "reason": why,
            "devices": [{"id": d.id, "model": d.model, "backend": d.backend, **({"options": d.options} if d.options else {})}
                        for d in self.config.devices],
            "limits": self.config.limits, "confirmed_by": (self.config.confirmation or {}).get("by")}

  def require_device(self) -> DeviceSpec:
    if self.problem is not None:
      raise LabError(self.problem.code, self.problem.message, self.problem.hint)
    if self.config is None:
      raise LabError("no_config", "this task folder has no AHC config yet.",
                     "Call configure_devices with the device the person chose.")
    status, why = self.config.status(self.forced_sim)  # an in-memory simulation config is confirmed
    if status != "confirmed" or self.spec is None:
      raise LabError("config_pending", f"the workspace config is not confirmed ({why}); nothing may move.",
                     "List available_models from lab_overview, let the person choose, write it with "
                     "configure_devices, then ask the person to confirm it with confirm_config.")
    return self.spec

  def next_step(self) -> str:
    if self.estop is not None:
      return "The emergency stop is engaged: stop, tell the person and wait. Only the person releases it."
    status = self.config_status()["status"]
    if status == "invalid":
      return "Fix .ahc/config.yaml (see config.reason) or rewrite it with configure_devices."
    plan = "" if self.plan.steps else "Report your plan with report_decision(plan=[...]) first; then "
    if status == "none" and self.forced_sim:
      return plan + "load_layout starts the simulation (configure_devices picks another model before that)."
    if status in ("none", "pending"):
      return ("List available_models to the person and let them choose; write the choice with configure_devices; "
              "then the person confirms it with confirm_config. Do not confirm it yourself.")
    if self.gate is not None and self.gate.state != "passed":
      if self.gate.needs_person and self.gate.state == "pending":
        return ("The person confirms this layout before anything moves: verify(check='deck_matches_layout') asks "
                "for their verdict; show them the layout and wait.")
      return f"The deck check is {self.gate.state}: verify(check='deck_matches_layout') before anything moves."
    if self.gate is None:
      return "find_layout_references; " + plan.lower() + "load_layout, then verify(check='deck_matches_layout')."
    if not self.plan.steps:
      return "Report your plan with report_decision(plan=[...]) before any action."
    return "Liquid handling is open; describe_device(component) for limits."

  # -- run log --------------------------------------------------------------------------------

  def record(self, entry: dict[str, Any]) -> None:
    if self.step_context is not None and ("op" in entry or entry.get("type") == "verification"):
      entry = {**entry, "plan_step": self.step_context}
    if self.runlog is None:
      model = self.spec.model if self.spec else (self.config.devices[0].model if self.config and self.config.devices else None)
      backend = self.adapter.backend if self.adapter else ("sim" if self.forced_sim else None)
      self.runlog = RunLog(self.ws.runs_dir, model, backend, extra={"workspace": str(self.ws.root)})
      if self.plan.steps is not None and "plan" not in entry:  # a new log (the device changed): the plan goes on
        carried: dict[str, Any] = {"type": "agent", "decision": "The plan goes on in a new run log", "plan": self.plan.steps,
                                   "visited": sorted(self.plan.visited)}
        if self.plan.current is not None:
          carried["current_step"] = self.plan.current
        self.runlog.record(carried)
    self.runlog.record(entry)
    self.plan.apply(entry)

  def summary(self) -> dict[str, Any]:
    if self.runlog is None:
      self.runlog = RunLog(self.ws.runs_dir, self.spec.model if self.spec else None,
                           self.adapter.backend if self.adapter else None, extra={"workspace": str(self.ws.root)})
    return self.runlog.summary()

  async def logged(self, op: str, args: dict[str, Any], fn: Callable[[], Awaitable[dict[str, Any]]]):
    """Run a non-liquid-handling operation and log it, refused or not."""
    try:
      result = await fn()
    except LabError as exc:
      self.record({"op": op, "status": "refused", "error": exc.code, "message": str(exc), "basic": args})
      raise
    except Exception as exc:  # noqa: BLE001
      error = LabError("device_refused", f"{type(exc).__name__}: {exc}")
      self.record({"op": op, "status": "refused", "error": error.code, "message": str(error), "basic": args})
      raise error from exc
    self.record({"op": op, "status": "ok", "basic": args, "effective": result})
    return result

  async def act(self, fn: Callable[[], Awaitable[dict[str, Any]]], op: str, args: dict[str, Any],
                plan_step: int, gated: bool = True) -> dict[str, Any]:
    """A tool that may move something: not stopped, config confirmed, a plan reported, deck check passed.

    The work runs as a child task the emergency stop can cancel; the request itself is never
    cancelled, so the client gets a proper refusal instead of a broken call. What the call records
    carries its plan step; a call refused here never starts its step.
    """
    async def body():
      await self.refresh(write=True)
      try:
        self.require_not_stopped()
        self.require_device()
        self.plan.check(plan_step)
        if gated and self.gate is not None:
          self.gate.require_open()
      except LabError as exc:  # safety refusals belong in the run log too
        self.record({"op": op, "device": args.get("device"), "basic": args, "status": "refused",
                     "error": exc.code, "message": str(exc)})
        raise
      last = self.last_step_starts(plan_step)
      before = self.lh._twin_volumes() if self.lh else {}
      self.step_context = plan_step
      task = asyncio.ensure_future(fn())
      self.current_action = task
      try:
        return self.plan_note(await task, last)
      except asyncio.CancelledError:
        if not (task.cancelled() and self.estop is not None):
          task.cancel()
          raise
        after = self.lh._twin_volumes() if self.lh else {}
        moved = {k: round(after[k] - v, 3) for k, v in before.items() if k in after and abs(after[k] - v) > 1e-6}
        error = LabError("emergency_stop",
                         f"the emergency stop interrupted {op} ({self.estop.get('by')})"
                         + (f"; partly done: {', '.join(f'{k} {d:+g} uL' for k, d in list(moved.items())[:8])}" if moved else "")
                         + ". Nothing runs until the person releases it.",
                         "Stop now and tell the person. Do not retry, work around it or release it yourself.")
        record = {"op": op, "device": args.get("device"), "basic": args, "status": "refused",
                  "error": error.code, "message": str(error)}
        if moved:
          record["partial"] = moved
        self.record(record)
        raise error from None
      finally:
        self.current_action = None
        self.step_context = None
    return await self.run(body)

  def last_step_starts(self, plan_step: int) -> bool:
    return bool(self.plan.steps) and plan_step == len(self.plan.steps) and plan_step not in self.plan.visited

  def plan_note(self, result: dict[str, Any], last: bool) -> dict[str, Any]:
    """The first call of the plan's last step reminds the agent how the plan ends."""
    if not last:
      return result
    n = len(self.plan.steps)
    return {**result, "plan": f"Step {n} of {n}, the last of your plan: when it is done, report the result with "
                              "report_decision(plan_done=true)."}

  def gate_changed(self, reason: str) -> None:
    self.record({"type": "gate", **self.gate.public(), "reason": reason})

  async def read(self, fn: Callable[[], Any]) -> Any:
    """A read-only tool: never creates anything in the workspace."""
    async def body():
      await self.refresh(write=False)
      return fn()
    return await self.run(body)

  # -- views ----------------------------------------------------------------------------------

  def overview(self) -> dict[str, Any]:
    out: dict[str, Any] = {"workspace": str(self.ws.root), "forced_simulation": self.forced_sim,
                           "config": self.config_status(),
                           "emergency_stop": {"engaged": True, **self.estop} if self.estop else {"engaged": False}}
    if self.spec is None:
      out["available_models"] = {m: {"title": (s := load_device(m)).title, "backends": list(s.backends)}
                                 for m in known_models()}
      out["checks"] = CHECKS
      out["next"] = self.next_step()
      return out
    spec = self.spec
    comps = {}
    for name, c in spec.components.items():
      entry: dict[str, Any] = {"title": c.title, "capabilities": list(c.capabilities)}
      if c.capabilities:
        entry.update(channels=c.channels, shared_container=c.shared_container, equal_volumes=c.equal_volumes)
      if c.note:
        entry["note"] = c.note
      comps[f"{spec.prefix}.{name}"] = entry
    out.update(gate=self.gate.public() if self.gate else None)
    out.update(device=spec.model, id=spec.prefix, title=spec.title, backend=self.adapter.backend,
               simulated=self.adapter.simulated, backend_detail=spec.backends[self.adapter.backend],
               components=comps, layout_format=_layout_format(spec), checks=CHECKS,
               live_view={"enabled": self.view is not None, "url": self.view.url if self.view else None},
               next=self.next_step())
    sim = SIMULATORS.get(spec.model) if self.adapter.simulated else None
    if sim is not None:
      out["simulator"] = {**sim.status(self.ws), "start": sim.start_command(self.ws),
                          "setup": f"{sim.ahc_sim_command()} ot2 --setup"}
    return out

  def state(self, device: str | None) -> dict[str, Any]:
    if self.spec is None:
      return {"configured": False, "config": self.config_status()}
    names = [device] if device else [n for n, c in self.spec.components.items() if c.capabilities]
    comps = [self.spec.component(n) for n in names]
    out: dict[str, Any] = {"simulated": self.adapter.simulated, "gate": self.gate.public() if self.gate else None,
                           "tips_mounted": {f"{self.spec.prefix}.{c.name}": len(self.adapter.mounted_tips(c))
                                            for c in comps}}
    layout = self.adapter.layout
    if layout is None:
      out["layout"] = None
      return out
    liquids: dict[str, Any] = {}
    tips_left: dict[str, int] = {}
    for name, res in layout.labware.items():
      if isinstance(res, TipRack):
        tips_left[name] = sum(1 for s in res.get_all_items() if s.has_tip())
      elif isinstance(res, Plate):
        held = {w.get_identifier(): round(w.tracker.get_used_volume(), 3) for w in res.get_all_items()
                if w.tracker.get_used_volume() > 0}
        if held:
          liquids[name] = held
      elif isinstance(res, Container):
        liquids[name] = round(res.tracker.get_used_volume(), 3)
    out.update(liquids_ul=liquids, tips_left=tips_left)
    return out


def _layout_format(spec: DeviceSpec) -> dict[str, Any]:
  kind = spec.layout["kind"]
  return {"kind": kind, "rules": {k: v for k, v in spec.layout.items() if k != "kind"},
          "example": LAYOUT_EXAMPLES[kind]}


def _model_description(spec: DeviceSpec) -> dict[str, Any]:
  return {"device": spec.model, "title": spec.title, "backends": spec.backends, "guide": spec.guide,
          "components": {f"{spec.prefix}.{n}": c.public() for n, c in spec.components.items()}}


def create_server(workspace=None, backend: str | None = None, device: str | None = None,
                  options: dict[str, Any] | None = None, view: bool | None = None) -> MCPServer:
  backend = backend if backend is not None else (os.environ.get("AHC_BACKEND") or None)
  if backend not in (None, "sim"):
    raise ValueError(f"backend {backend!r} cannot be forced: only 'sim' can. Real backends come only from "
                     "a workspace config the person confirmed.")
  if view is None:
    view = os.environ.get("AHC_VIEW") == "1"
  lab = Lab(resolve_workspace(workspace), forced_sim=backend == "sim",
            default_model=device or os.environ.get("AHC_DEVICE") or None, options=options or {}, view=view)

  @asynccontextmanager
  async def lifespan(_server: MCPServer):
    watcher = asyncio.create_task(lab.watch_estop())
    try:
      yield lab
    finally:
      watcher.cancel()
      await lab.shutdown()

  server = MCPServer("ahc", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)
  server.lab = lab  # tests and embedding code reach the live state here

  @server.tool(annotations=READ_ONLY,
               description="Start here: this task's workspace and device config, the device and its components, the layout format, and the next step.")
  async def lab_overview() -> dict[str, Any]:
    return await lab.read(lab.overview)

  @server.tool(annotations=CONFIGURE,
               description="Write this task's device config (.ahc/config.yaml) after the person chose the device: devices (id, model, backend) and optional stricter task limits. "
                           "It stays pending until the person confirms it with confirm_config; a simulation-only config is confirmed automatically when the server forces simulation.")
  async def configure_devices(devices: DeviceList, limits: TaskLimits = None) -> dict[str, Any]:
    async def body():
      lab.require_not_stopped()
      entries = parse_devices(devices)
      if not entries:
        raise LabError("config_invalid", "devices is empty.", "Name the device the person chose.")
      for e in entries:  # refuse limits that would loosen the description file's, before writing
        apply_limits(replace(load_device(e.model), id=e.id), limits or {})
      text = lab.ws.read_config_text()
      try:
        previous = parse_config(text) if text else None
      except LabError:
        previous = None
      config = Config(devices=entries, limits=limits or {}, sim=previous.sim if previous else {})
      # Simulation-only and forced: confirmed without the person, unless it drops or loosens task
      # limits the person set, which only the person may do.
      keeps = keeps_limits(previous.limits if previous else {}, config.limits)
      if lab.forced_sim and all(e.backend == "sim" for e in entries) and keeps:
        config.confirm("simulation")
      lab.ws.write_config(config.data(), HEADER)
      lab.default_model = None  # the explicit choice supersedes --device
      await lab.refresh(write=False)
      out = {"config": lab.config_status(), "next": lab.next_step()}
      if not keeps:
        out["next"] = ("This drops or loosens task limits the person set, so it waits for the person: ask them to "
                       "confirm it with confirm_config. Do not confirm it yourself.")
      return out
    return await lab.run(lambda: lab.logged("configure_devices", {"devices": devices, "limits": limits}, body))

  @server.tool(annotations=PERSON_ONLY,
               description="For the person on site only: confirm this task's device config so that motion is allowed. Never call or auto-approve this on your own; "
                           "ask the person, who approves this call themselves. A real backend runs only from a config confirmed here.")
  async def confirm_config() -> dict[str, Any]:
    async def body():
      lab.require_not_stopped()
      await lab.refresh(write=False)
      if lab.problem is not None:
        raise LabError(lab.problem.code, lab.problem.message, lab.problem.hint)
      if lab.config is None or not lab.config.devices:
        raise LabError("nothing_to_confirm", "the config names no device yet.", "Call configure_devices first.")
      for e in lab.config.devices:  # never confirm limits that would loosen the description file's
        apply_limits(replace(load_device(e.model), id=e.id), lab.config.limits)
      lab.config.confirm("human")
      lab.ws.write_config(lab.config.data(), HEADER)
      await lab.refresh(write=False)
      return {"config": lab.config_status(), "next": lab.next_step()}
    return await lab.run(lambda: lab.logged("confirm_config", {}, body))

  @server.tool(annotations=READ_ONLY,
               description="Limits, constraints and specialized operations of one component; without a component, the device guide. A model name (e.g. hamilton.starlet) describes that model before it is configured.")
  async def describe_device(component: Annotated[str | None, Field(description="e.g. starlet.pip, or a model name; omitted = whole device")] = None) -> dict[str, Any]:
    def body():
      if component in known_models():
        return _model_description(load_device(component))
      spec = lab.require_device()
      if component is None:
        return _model_description(spec)
      comp = spec.component(component)
      out = comp.public()
      out["specialized_params"] = {op: sorted(params) for op, params in comp.specialized.items() if params}
      return out
    return await lab.read(body)

  @server.tool(annotations=READ_ONLY,
               description="Every parameter of one operation on one component, each with its limits (task limits included) and source.")
  async def get_params(device: Device, op: Annotated[str, Field(description="pick_up_tips, aspirate, dispense, mix or drop_tips")]) -> dict[str, Any]:
    def body():
      lab.require_device()
      comp = lab.lh.component(device)
      if op not in LIQUID_HANDLING_OPS:
        raise LabError("unknown_op", f"{op!r} is not a liquid-handling operation.", f"Ops: {', '.join(LIQUID_HANDLING_OPS)}.")
      basic = {}
      for name, meta in BASIC_PARAMS[op].items():
        entry = {k: v for k, v in meta.items() if k != "limit"}
        if "limit" in meta:
          entry["limits"] = comp.limits[meta["limit"]].public()
        basic[name] = entry
      out: dict[str, Any] = {"device": device, "op": op, "basic": basic,
                             "specialized": {k: p.public() for k, p in comp.specialized.get(op, {}).items()}}
      if op in comp.translations:
        out["translation"] = comp.translations[op]
      constraints = []
      if comp.equal_volumes and comp.channels > 1:
        constraints.append("every channel moves the same volume")
      if not comp.shared_container and comp.channels > 1:
        constraints.append("one distinct well per channel")
      if constraints:
        out["constraints"] = constraints
      return out
    return await lab.read(body)

  @server.tool(annotations=READ_ONLY,
               description="Before writing a layout: the protocols in this workspace's .ahc/protocols/, the layouts saved in .ahc/layouts/, and the layouts earlier runs here used. Only this workspace is searched.")
  async def find_layout_references() -> dict[str, Any]:
    def body():
      refs = lab.ws.references()
      if any(refs[k] for k in ("protocols", "layouts", "run_layouts")):
        refs["next"] = "Base the new layout on these. load_layout saves it in .ahc/layouts/."
      elif lab.adapter is not None and lab.adapter.simulated:
        refs["next"] = ("No references in this workspace. In simulation start from lab_overview's "
                        "layout_format.example; the person confirms that layout before anything moves (the deck "
                        "check waits for their verdict). On real hardware ask the person on site first.")
      else:
        refs["next"] = ("No references in this workspace: ask the person on site to describe or check the physical "
                        "deck (labware, positions, liquids) before writing a layout. Physical verification help "
                        "is a person for now; cameras later.")
      return refs
    return await lab.read(body)

  @server.tool(annotations=MOTION,
               description="Load a deck layout (labware, positions, starting liquids, aliases) and save it in .ahc/layouts/, or load a saved one by name. "
                           "See lab_overview for the format and find_layout_references for this workspace's earlier layouts. Resets the simulated device. "
                           "In simulation, a layout that follows no reference in this task folder is saved only once the person confirms it.")
  async def load_layout(plan_step: PlanStep,
                        layout: Annotated[dict[str, Any] | None, Field(description="Device-specific layout; see lab_overview.layout_format.example")] = None,
                        name: Annotated[str | None, Field(description="With layout: save it under this name (default layout-N). Without layout: load that saved layout.")] = None) -> dict[str, Any]:
    async def load(chosen):
      if chosen is None:
        raise LabError("layout_required", "give a layout, or the name of a saved one.",
                       "find_layout_references lists the saved layouts.")
      if layout is not None and name is not None and lab.ws.layout_path(name).is_file() \
          and lab.ws.read_layout(name) != layout:
        raise LabError("layout_exists", f"a different layout is already saved as {name!r}.",
                       "Pick another name; saved layouts are never overwritten.")
      # Nothing in this folder to base it on (the built-in example, or the agent's own): in simulation the
      # person confirms it, as on real hardware. It becomes a reference (saved) only then.
      needs_person = layout is not None and lab.adapter.simulated and not lab.ws.has_reference()
      built = await lab.adapter.load_layout(chosen)
      built.record_placements()
      saved_as = name if layout is None else (name or lab.ws.next_layout_name())
      if layout is not None and not needs_person:
        lab.ws.save_layout(saved_as, layout, lab.spec.model)
      lab.gate = Gate(layout=saved_as, needs_person=needs_person, pending_layout=layout if needs_person else None)
      out = {**built.summary(), "saved_as": saved_as, "path": str(lab.ws.layout_path(saved_as)), "gate": lab.gate.public()}
      if needs_person:
        out.update(needs_person=True, saved=False,
                   next=("This layout follows no reference in this task folder, so the person confirms it before anything "
                         "moves: verify(check='deck_matches_layout') asks for their verdict. It is saved in .ahc/layouts/ "
                         "once they confirm it."))
      else:
        out["next"] = "verify(check='deck_matches_layout') before anything moves."
      return out

    async def body():
      chosen = layout if layout is not None else (lab.ws.read_layout(name) if name else None)
      result = await lab.logged("load_layout", {"layout": chosen, "name": name}, lambda: load(chosen))
      lab.gate_changed("layout loaded")
      root = lab.adapter.view_root()
      if lab.view is not None and root is not None:
        lab.view.title = f"AHC · {lab.spec.title}"
        result = {**result, "live_view_url": await lab.view.show(root)}
      return result
    return await lab.act(body, "load_layout", {"layout": layout, "name": name}, plan_step, gated=False)

  @server.tool(annotations=READ_ONLY, description="Tips mounted, liquid in each container and tips left, from the digital twin.")
  async def get_state(device: Annotated[str | None, Field(description="Component; omitted = all")] = None) -> dict[str, Any]:
    return await lab.read(lambda: lab.state(device))

  @server.tool(annotations=MOTION, description="Pick up tips on a component.")
  async def pick_up_tips(device: Device, plan_step: PlanStep,
                         tips: Annotated[list[str] | None, Field(description="e.g. ['tips:A1:H1']; omitted = next fresh")] = None) -> dict[str, Any]:
    return await lab.act(lambda: lab.lh.pick_up_tips(device, tips), "pick_up_tips", {"device": device, "tips": tips},
                         plan_step)

  @server.tool(annotations=MOTION, description="Aspirate from targets with the mounted tips.")
  async def aspirate(device: Device, targets: Targets, volumes: Volumes, plan_step: PlanStep, flow_rate: FlowRate = None,
                     liquid_height: Height = None, specialized: Specialized = None) -> dict[str, Any]:
    return await lab.act(lambda: lab.lh.aspirate(device, targets, volumes, flow_rate, liquid_height, specialized),
                         "aspirate", {"device": device, "targets": targets, "volumes": volumes}, plan_step)

  @server.tool(annotations=MOTION, description="Dispense into targets from the mounted tips.")
  async def dispense(device: Device, targets: Targets, volumes: Volumes, plan_step: PlanStep, flow_rate: FlowRate = None,
                     liquid_height: Height = None, specialized: Specialized = None) -> dict[str, Any]:
    return await lab.act(lambda: lab.lh.dispense(device, targets, volumes, flow_rate, liquid_height, specialized),
                         "dispense", {"device": device, "targets": targets, "volumes": volumes}, plan_step)

  @server.tool(annotations=MOTION, description="Mix in place in the targets with the mounted tips.")
  async def mix(device: Device, targets: Targets,
                volume: Annotated[float, Field(description="uL drawn and expelled each cycle")],
                repetitions: Annotated[int, Field(description="cycles")], plan_step: PlanStep, flow_rate: FlowRate = None,
                liquid_height: Height = None, specialized: Specialized = None) -> dict[str, Any]:
    return await lab.act(lambda: lab.lh.mix(device, targets, volume, repetitions, flow_rate, liquid_height, specialized),
                         "mix", {"device": device, "targets": targets, "volume": volume, "repetitions": repetitions},
                         plan_step)

  @server.tool(annotations=MOTION, description="Drop the mounted tips: discard into the trash, or return them.")
  async def drop_tips(device: Device, plan_step: PlanStep, mode: Literal["discard", "return"] = "discard") -> dict[str, Any]:
    return await lab.act(lambda: lab.lh.drop_tips(device, mode), "drop_tips", {"device": device, "mode": mode}, plan_step)

  @server.tool(annotations=MOTION, description="Run a model-only operation listed in describe_device (e.g. sense_tip_presence on starlet.pip).")
  async def invoke(device: Device, op: str, plan_step: PlanStep, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async def body():
      comp = lab.spec.component(device)
      return await lab.adapter.invoke(comp, op, params or {})
    return await lab.act(lambda: lab.logged(f"invoke:{op}", {"device": device, "params": params}, body),
                         f"invoke:{op}", {"device": device, "params": params}, plan_step)

  @server.tool(annotations=READ_ONLY, description="Physical-world checks this server can gather evidence for.")
  async def list_checks() -> dict[str, Any]:
    def body():
      judged = ("depends on the device; configure it first" if lab.adapter is None
                else "simulation" if lab.adapter.simulated else "human (placeholder)")
      return {"checks": CHECKS, "judged_by": judged}
    return await lab.read(body)

  @server.tool(annotations=GATE,
               description="Gather evidence for a check (digital twin, sensors, camera) and return a verdict. deck_matches_layout also opens or closes the motion gate after load_layout, "
                           "so this is not read-only. On real hardware, and in simulation for a layout without a reference, the verdict waits for the person.")
  async def verify(check: Annotated[str, Field(description="tips_mounted, liquid_present or deck_matches_layout")],
                   plan_step: PlanStep,
                   device: Annotated[str | None, Field(description="Component, for tips_mounted")] = None,
                   targets: Annotated[list[str] | None, Field(description="For liquid_present")] = None,
                   expect: Annotated[Literal["mounted", "none"], Field(description="For tips_mounted")] = "mounted",
                   min_volume_ul: float = 0.0) -> dict[str, Any]:
    async def body():
      lab.require_not_stopped()
      await lab.refresh(write=False)
      lab.require_device()
      lab.plan.check(plan_step)
      last = lab.last_step_starts(plan_step)
      deck = check == "deck_matches_layout" and lab.gate is not None
      lab.step_context = plan_step
      try:
        result = await lab.verification.verify(check, device, targets, expect, min_volume_ul,
                                               person=deck and lab.gate.needs_person)
      finally:
        lab.step_context = None
      if deck:
        lab.gate.apply(result["check_id"], result["verdict"])
        lab.gate_changed(f"deck check {result['verdict']}")
        result = {**result, "gate": lab.gate.public()}  # a copy: the pending check is stored as it was
      return lab.plan_note(result, last)
    return await lab.run(body)

  @server.tool(annotations=PERSON_ONLY,
               description="For the person only: record pass or fail for a check that waits for them (the deck on real hardware; in simulation, a layout without a reference). "
                           "Call it only with the verdict the person gave you; never decide or auto-approve it yourself.")
  async def record_verdict(check_id: str, verdict: Literal["pass", "fail"], note: str = "") -> dict[str, Any]:
    async def body():
      lab.require_not_stopped()
      await lab.refresh(write=False)
      lab.require_device()
      result = lab.verification.record_verdict(check_id, verdict, note)
      gate = lab.gate
      if result["check"] == "deck_matches_layout" and gate is not None and gate.check_id == check_id:
        gate.apply(check_id, verdict)
        if verdict == "pass" and gate.pending_layout is not None:  # confirmed: now a reference for this folder
          path = lab.ws.save_layout(gate.layout, gate.pending_layout, lab.spec.model)
          gate.pending_layout, gate.needs_person = None, False
          result = {**result, "saved_as": gate.layout, "path": str(path)}
        elif verdict == "fail" and gate.needs_person:
          result = {**result, "next": "Ask the person what to change, then load the corrected layout; they confirm it again."}
        lab.gate_changed(f"the person recorded {verdict}")
        result = {**result, "gate": gate.public()}
      return result
    return await lab.run(body)

  @server.tool(annotations=NOTE,
               description="Tell the person watching what you decided and why, in one line each. First, before load_layout or any action: your plan, as short concrete steps in `plan` "
                           "(not in the decision text); every load_layout, verify and action call then names its step with plan_step, and the dashboard ticks a step once you move on. "
                           "Also report a changed plan, a refusal that changes what you do, and waiting for the person (waiting_for='person', with current_step if that is a step of its own). "
                           "When the last step is done, report the result with plan_done=true. Shown on the run dashboard and kept in this task's run log (so it writes .ahc/runs).")
  async def report_decision(decision: Annotated[str, Field(description="What you decided to do now, one line")],
                            why: Annotated[str | None, Field(description="Why, one line")] = None,
                            plan: Plan = None,
                            current_step: Annotated[int | None, Field(description="A step that starts without a call of its own (e.g. waiting for the person); calls name theirs with plan_step")] = None,
                            waiting_for: Annotated[Literal["person"] | None, Field(description="Set while you wait for the person")] = None,
                            plan_done: Annotated[bool, Field(description="The plan is finished: this report gives the result")] = False) -> dict[str, Any]:
    async def body():
      if not decision.strip():
        raise LabError("bad_decision", "decision is empty.", "Say in one line what you decided to do.")
      if plan is not None and (not plan or len(plan) > MAX_STEPS or any(not isinstance(step, str) or not step.strip() for step in plan)):
        raise LabError("bad_plan", f"plan must be 1 to {MAX_STEPS} non-empty steps.", "Give the plan as short step texts.")
      steps = plan if plan is not None else lab.plan.steps
      if (current_step is not None or plan_done) and steps is None:
        raise LabError("no_plan", "there is no plan to report progress on.", "Report the plan first with plan=[...].")
      if current_step is not None and not 1 <= current_step <= len(steps):
        raise LabError("bad_step", f"current_step must be a step of the plan (1-{len(steps)}).")
      await lab.refresh(write=False)
      entry: dict[str, Any] = {"type": "agent", "decision": decision.strip()[:500]}
      if why:
        entry["why"] = why.strip()[:500]
      if plan is not None:
        entry["plan"] = [step.strip()[:200] for step in plan]
      if current_step is not None:
        entry["current_step"] = current_step
      if waiting_for:
        entry["waiting_for"] = waiting_for
      if plan_done:
        entry["plan_done"] = True
      lab.record(entry)
      result: dict[str, Any] = {"recorded": True, "shown": "on the run dashboard (ahc-dashboard) and in the run log"}
      if plan is not None:
        result["next"] = (f"Plan recorded ({len(plan)} steps). Pass plan_step (the step a call belongs to) on every "
                          "load_layout, verify and action call; a step is ticked once you move on to a later one. When "
                          "the last step is done, report the result with report_decision(plan_done=true).")
      return result
    return await lab.run(body)

  @server.tool(annotations=READ_ONLY,
               description="This session's run log summary: calls, which needed specialized parameters or translation, refusals, commands sent.")
  async def get_run() -> dict[str, Any]:
    return await lab.read(lab.summary)

  @server.resource("ahc://device", description="The full description file of the configured device.")
  def device_file() -> str:
    if lab.spec is None:
      return "No device is configured in this workspace yet. Models: " + ", ".join(known_models())
    return lab.spec.path.read_text()

  return server


def main() -> None:
  parser = argparse.ArgumentParser(description="AHC lab hardware MCP server (one task workspace, one device).")
  parser.add_argument("--workspace", default=None, help="task folder (default: AHC_WORKSPACE, else the current folder)")
  parser.add_argument("--backend", choices=["sim"], default=None, help="force simulation (also AHC_BACKEND=sim)")
  parser.add_argument("--device", default=None, help="model for a generated simulation config (also AHC_DEVICE)")
  parser.add_argument("--host", default=os.environ.get("AHC_OT2_HOST"), help="OT-2 robot-server host override")
  parser.add_argument("--port", type=int, default=int(os.environ["AHC_OT2_PORT"]) if os.environ.get("AHC_OT2_PORT") else None,
                      help="OT-2 robot-server port override")
  parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
  parser.add_argument("--http-port", type=int, default=8765)
  parser.add_argument("--view", action="store_true", default=os.environ.get("AHC_VIEW") == "1",
                      help="open a live 3D view of the device in the browser (also AHC_VIEW=1)")
  args = parser.parse_args()
  try:
    server = create_server(args.workspace, args.backend, args.device, {"host": args.host, "port": args.port},
                           view=args.view)
  except ValueError as exc:
    parser.error(str(exc))
  if args.transport == "stdio":
    server.run("stdio")
  else:
    server.run("streamable-http", host="127.0.0.1", port=args.http_port)


if __name__ == "__main__":
  main()
