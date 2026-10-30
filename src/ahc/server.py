"""The MCP server: one task workspace, one device, a small fixed tool set, limits next to every parameter.

The server runs in a task folder (or --workspace / AHC_WORKSPACE) and writes nothing until a tool
needs to; the task's config, layouts and run logs then live under <folder>/.ahc/. Which device it
drives comes from that config, never from the client: `--backend sim` can only force simulation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
import ipaddress
import json
import math
import os
import shlex
import signal
import sys
import tempfile
import time
import urllib.error
import uuid
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field
from pylabrobot.resources import Container, set_tip_tracking, set_volume_tracking

from ahc import __version__, discovery, vision
from ahc import auto_verify as auto
from ahc import physical_world as pw
from ahc import vision_points as vpoints
from ahc import cameras as cams
from ahc.core.capability import LiquidHandling
from ahc.core import deck_state
from ahc.core.errors import LabError
from ahc.core.layout import deck_differences
from ahc.devices.base import Adapter
from ahc.devices.hamilton.starlet import STARletAdapter
from ahc.devices.opentrons.ot2 import OT2Adapter
from ahc.devices.opentrons.sim import robot_server as ot2_sim
from ahc.devices.spec import LIQUID_HANDLING_OPS, DeviceSpec, device_spec, known_models, load_device
from ahc.plan import MAX_STEPS, PlanProgress
from ahc.records import RunLog
from ahc.timing import run_timing
from ahc.verification import CHECKS, Gate, Verification
from ahc.verification.ask import HOMING, agent_now, confirm_now, pass_now
from ahc.verification.checks import ON_A_REAL_DECK
from ahc.instrument_context import InstrumentContext
from ahc.vision_context import VisionContext
from ahc.vision_points import VisionRecord
from ahc.viz import dashboard as run_dashboard
from ahc.workspace import (CameraEntry, Config, DeviceEntry, Workspace, apply_limits, parse_auto_verify, parse_cameras, parse_config,
                           parse_devices, parse_slot_map, resolve_workspace, simulation_config, slots_text, template_text)
from ahc.workspace.config import HEADER
from ahc.workspace import estop
from ahc.workspace.store import write_atomic
from ahc.workspace.limits import keeps_limits
from ahc.workspace.robot_lock import RobotLock, claude_session

ADAPTERS = {"hamilton.starlet": STARletAdapter, "opentrons.ot2": OT2Adapter}
# Models whose simulation runs as a separate per-task process (ahc-sim); the STARlet's is in-process.
SIMULATORS = {"opentrons.ot2": ot2_sim}
DEFAULT_SIM_MODEL = "hamilton.starlet"
REPO = Path(__file__).resolve().parents[2]  # the checkout the plugin runs with `uv run --project`
DASHBOARD_PORT = 8770  # the first port open_dashboard tries; the next free ones follow
DECK_OPS = ("load_layout", "pick_up_tips", "aspirate", "dispense", "mix", "drop_tips")  # calls that change the deck
DEFAULT_PORT = 31950  # the OT-2 robot-server's port when an entry names none
SIGNAL_SHUTDOWN_S = 10.0  # a signalled server ends its device's run for at most this long, then exits anyway
ROBOT_SEARCH_S = 3.0  # how long a robot that does not answer is looked for by its name (find_robots listens this long)
ROBOT_SEARCH_BOUND_S = 10.0  # ... and the most the call waits for that search, mDNS lookups included
ROBOT_SEARCH_KEPT_S = 30.0  # a search result is reused for this long, so a robot that stays off is not searched at every call
# The stop went out but the robot never said its run stopped (not yet, or it refused): never "stopped" before it does.
STOP_UNCONFIRMED = ("The emergency stop was sent to the robot, which has not confirmed that its run stopped: it may still "
                    "be executing its last command, and the power switch is the immediate stop")


DECK_GUIDE = ("OT-2 deck seen from above: slots 1-3 are the front row left to right, 4-6 the row behind, 7-9 the next, "
              "then 10 and 11 and the trash 12 in the back row. Each slot's labware footprint is 128 x 85 mm, long side "
              "left to right; the slot's number is engraved on the deck and its guide lines mark the corners.")


def write_bytes_atomic(path: Path, data: bytes) -> None:
  """Bytes into `path` through a temporary file and a rename: a reader never sees half an image."""
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
  try:
    tmp.write_bytes(data)
    os.replace(tmp, path)
  except BaseException:
    tmp.unlink(missing_ok=True)
    raise


def search_robots(timeout_s: float) -> list[dict[str, Any]]:
  """find_robots (read-only: mDNS and GET /health), for the search by name; blocking. Tests replace this."""
  return discovery.find_robots(timeout_s)


def check_entry(entry: DeviceEntry) -> None:
  """A device entry as its adapter checks it: its own backend and options, never server-level overrides."""
  ADAPTERS[entry.model].check_options(entry.backend, entry.options)


async def probe_entry(entry: DeviceEntry) -> tuple[DeviceEntry, dict[str, Any]]:
  """A real device, read before the person is asked to confirm it: who answers at that address and what
  its mounts carry (read-only: no run, no motion). Returns the entry with robot_name and mounts filled in
  from the robot itself, and the identity to show the person. Refuses a different robot, mounts the entry
  states that the robot does not carry, and a robot with no pipette AHC drives."""
  probe = getattr(ADAPTERS[entry.model], "probe_identity", None)
  if entry.backend == "sim" or probe is None:
    return entry, {}
  try:
    identity = await asyncio.to_thread(probe, entry.backend, dict(entry.options))
  except LabError as exc:
    if exc.code != "backend_unreachable":
      raise
    raise LabError(exc.code, exc.message + " Nothing was written.",
                   (exc.hint + " " if exc.hint else "") + "configure_devices reads the robot's name and pipettes before "
                   "it writes the config, so the robot must be on and reachable; ask the person.") from exc
  if not identity:
    return entry, {}
  where = f"{entry.options.get('host')}:{entry.options.get('port') or DEFAULT_PORT}"
  name, expected = identity.get("name"), entry.options.get("robot_name")
  if expected and name != expected:
    raise LabError("wrong_robot", f"{where} is the robot {name!r}, not {expected!r}; nothing was written.",
                   "Check the robot's address and name with the person.")
  options = dict(entry.options)
  pipettes = identity.get("pipettes")
  if isinstance(pipettes, dict):
    found = {m: (p or {}).get("name") for m, p in pipettes.items()}
    stated = options.get("mounts")
    if stated is not None and {m: stated.get(m) for m in found} != found:
      raise LabError("mount_mismatch", f"{where} ({name}) carries {_mounts_text(found)}, but this entry names "
                     f"{_mounts_text(stated)}; nothing was written.",
                     "Leave mounts out of the entry: configure_devices reads them from the robot. If the robot should "
                     "carry other pipettes, tell the person: only a person changes them.")
    spec = device_spec(entry.model, {"mounts": found})
    if spec.raw.get("mounts") and not any(c.instrument for c in spec.components.values()):
      raise LabError("no_usable_pipette", f"{where} ({name}) carries {_mounts_text(found)}: none of them is a pipette "
                     f"AHC drives; nothing was written.",
                     f"AHC drives: {', '.join(spec.instruments)}. Tell the person: a pipette of one of these on a mount "
                     "makes the robot usable.")
    options["mounts"] = found
  if name and not expected:
    options["robot_name"] = name
  return replace(entry, options=options), identity


def _mounts_text(mounts: dict[str, Any]) -> str:
  """{'left': 'p300_single_gen2', 'right': None} -> "left 'p300_single_gen2', right empty"."""
  return ", ".join(f"{m} {name!r}" if name else f"{m} empty" for m, name in sorted(mounts.items())) or "no mounts"


def _loopback(host: str) -> bool:
  if host == "localhost":
    return True
  try:
    return ipaddress.ip_address(host).is_loopback
  except ValueError:
    return False


def _number(value: Any) -> str:
  return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else json.dumps(value)


MAX_DIFFERENCES = 12  # listed for the person; the rest are counted


def differs_text(match: dict[str, Any]) -> str | None:
  """How a layout differs from the task folder's references, in one or two sentences for the person; None when it
  matches one."""
  if "matches" in match:
    return None
  if "unreadable" in match:
    return f"This task folder's references could not be read ({match['unreadable']})."
  if "closest" not in match:
    return "No layout saved or run in this task folder to compare it with."
  shown = match["differences"][:MAX_DIFFERENCES]
  more = len(match["differences"]) - len(shown)
  return (f"It differs from the closest reference in this task folder, {match['closest']}: " + "; ".join(shown)
          + (f"; and {more} more" if more else "") + ".")


def _tips_count(mounted: Any) -> str:
  """{'ot2.right': 8} -> '8 on ot2.right'; a count nobody knows says so."""
  if not isinstance(mounted, dict) or not mounted:
    return "how many, and on which pipette, is unknown"
  return ", ".join(f"{'an unknown number' if n == 'unknown' else n} on {c}" for c, n in mounted.items())


def _mounts_summary(d: DeviceEntry) -> str:
  """", mounts left X, right Y" for a device whose model has mounts, else "". An empty mount reads "empty", one
  AHC does not drive "X (not driven)". Never raises: a broken option is shown as written."""
  try:
    spec = device_spec(d.model, d.options)
  except (LabError, ValueError, TypeError, AttributeError):
    given = d.options.get("mounts")
    return f", mounts {json.dumps(given, sort_keys=True, default=str)}" if given is not None else ""
  if not spec.mounted:
    return ""
  not_driven = spec.not_driven()
  return ", mounts " + ", ".join(f"{m} {(f'{n} (not driven)' if m in not_driven else n) if n else 'empty'}"
                                 for m, n in spec.mounted.items())


def confirm_summary(config: Config) -> str:
  """One line naming what a confirmation covers. confirm_config takes it back exactly, so the approval
  prompt shows the person the devices, where a real one is, and the task limits they confirm."""
  devices = []
  for d in config.devices:
    text = f"{d.id} = {d.model}, backend {d.backend}"
    if d.backend != "sim":
      name = d.options.get("robot_name")
      text += (f", robot {name!r}" if name else ", robot (no name)")
      if d.options.get("host"):
        text += f" at {d.options['host']}:{d.options.get('port') or DEFAULT_PORT}"
    text += _mounts_summary(d)
    devices.append(text)
  limits = []
  for address, params in sorted((config.limits or {}).items()):
    for key, bound in sorted(params.items()) if isinstance(params, dict) else [("", params)]:
      if isinstance(bound, dict) and set(bound) <= {"min", "max"}:
        limits.append(f"{address} {key} " + " ".join(f"{k} {_number(bound[k])}" for k in ("min", "max") if k in bound))
      else:
        limits.append(f"{address} {key} {json.dumps(bound, sort_keys=True)}".replace("  ", " "))
  text = "; ".join(devices) + "; task limits: " + (", ".join(limits) or "none")
  if config.cameras:
    text += "; cameras: " + ", ".join(f"{c.name} {c.device!r}" + (f" (sees {c.sees()})" if c.slot_map else "")
                                      for c in config.cameras)
  if config.auto_verify:
    text += "; auto-verify: " + config.auto_verify.summary()
  return text


INSTRUCTIONS = """\
Lab hardware MCP (AHC): call lab_overview first (this task's workspace, config, next step), then open_dashboard so the person can watch and stop the run.
Config: {config}
The person's tools (confirm_config, record_verdict): their approval in the client's dialog is the confirmation, so call them right away; do not wait for a typed answer. A pending check (verify gives ask_person): record_verdict(check_id, verdict='pass', note=<ask_person>). The config: confirm_config(summary=config.confirm_summary). Denied means not confirmed: stop, ask the person what they see, record_verdict(verdict='fail') with their words as the note. Never approve for them or invent their words. A client with no approval dialog on each call: ask in chat first.
Flow: lab_overview -> find_layout_references -> report_decision(plan=[...]) -> load_layout -> verify(check='deck_matches_layout') -> pick_up_tips / aspirate / dispense / mix / drop_tips -> verify -> report_decision(plan_done=true). get_params(device, op) only for device-specific behaviour.
Plan first: before load_layout or any action, report your plan as short concrete steps with report_decision (refused otherwise: plan_required). Every later call names its step with plan_step.
After every load_layout nothing moves until the deck check passes: the simulator judges it in simulation, the person on site on real hardware. A layout matching none saved or run in this task folder needs the person in simulation too (verify gives ask_person).
Address a component as <device id>.<component> (starlet.pip, ot2.right). Targets, volumes and limits are described with each tool's parameters and checked server-side before anything moves. Results marked simulated are not measurements.
With cameras: capture(label, plan_step) around the steps you want to check, and look; physical_world_context says what they can verify (lab-hardware skill).
Tell the person watching what you decide with report_decision: the plan, a changed plan, a refusal that changes your course, and waiting for the person's approval (waiting_for='person', just before the call they approve). One line each.
If a call is refused with emergency_stop, stop at once: do not retry or work around it, never release it yourself, tell the person and wait (report_decision with waiting_for='person'). Read-only calls, capture and report_decision still run, nothing else."""

CONFIG_FORCED = ("This server forces simulation: a simulation config is created on first use (configure_devices picks "
                 "another model). A config that drops or loosens the person's task limits needs their approval: call "
                 "confirm_config(summary=config.confirm_summary) right away.")
CONFIG_PERSON = ("With no config, ask the person whether to simulate or use a real robot, and which model "
                 "(available_models); write their choice with configure_devices; then call "
                 "confirm_config(summary=config.confirm_summary) right away. Nothing moves before it is approved. Each new "
                 "server session also needs their go-ahead, the same confirm_config, on a real robot or in a folder that "
                 "has never run an experiment (lab_overview.session).")
REAL_HARDWARE = """
Real hardware (a person on site):
- Find the robot with find_robots or ask the person for its address; never probe addresses yourself. configure_devices reads its name and mounts: the person confirms that identity.
- load_layout starts a run and homes the robot: hands and deck must be clear. Fold that into the dialog before it (the tips_mounted note or confirm_config): say in chat, in the same message before that call, that approving also means hands and deck are clear and the robot homes next. With no such dialog, ask in chat.
- Every verify on real hardware waits for the person, except volumes_match (the digital twin's answer): it returns pending with ask_person; report waiting_for='person', then record_verdict right away (auto: true: agent_verdict).
- On any device error (device_refused, completion_unknown, mount_mismatch, wrong_robot) or motion nobody expected: stop and ask the person; never retry a motion call.
- After an emergency stop, an unknown outcome or a restart with tips on, the person removes any tips: load_layout waits for their pass on verify(check='tips_mounted', expect='none') without device.
- Never reach the robot except through these tools: no curl or other HTTP to it, no ahc-smoke; only the person releases a stop (never ahc-estop --release)."""


def instructions(forced_sim: bool) -> str:
  """What every client is told; a server that may drive real hardware adds its rules."""
  if forced_sim:
    return INSTRUCTIONS.replace("{config}", CONFIG_FORCED)
  return INSTRUCTIONS.replace("{config}", CONFIG_PERSON) + REAL_HARDWARE

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MOTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
CONFIGURE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
NOTE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
GATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
RELEASE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PERSON_ONLY = ToolAnnotations(title="Person on site only - never auto-approve", readOnlyHint=False,
                              destructiveHint=True, idempotentHint=False, openWorldHint=False)
# Claude Code (2.1.199+) then asks the person on every call, even in auto or bypass mode and despite allow
# rules, and denies the call where no person can answer (dontAsk, claude -p). Other clients ignore it.
ASK_PERSON = {"anthropic/requiresUserInteraction": True}

BASIC_PARAMS = {
  "pick_up_tips": {"tips": {"type": "list[str] | null", "doc": "Tip spots such as tips:A1:H1 or tips:col1; omitted = the next fresh column (or tip for one channel) of a rack whose tips this pipette takes."}},
  "aspirate": {"targets": {"type": "list[str]", "doc": "One target per mounted tip (plate:col2 is A2-H2), or one container all channels share."},
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
     "sites": {"0": {"name": "buffer", "type": "Hamilton_1_trough_200ml_Vb"}}}],
    "liquids": {"buffer": 100000}, "aliases": {}},
  "slots": {"slots": {"1": {"name": "tips", "type": "opentrons_96_tiprack_300ul"},
                      "2": {"name": "reagents", "type": "cor_96_wellplate_2mL_Vb"},
                      "3": {"name": "plate", "type": "Cor_96_wellplate_360ul_Fb"},
                      "4": {"name": "tips20", "type": "opentrons_96_tiprack_20ul"}},
            "liquids": {"reagents:A1:H1": 1800}, "aliases": {"buffer": "reagents:A1:H1"}},
}


# Tool parameter types (module level so the SDK can resolve them).
Device = Annotated[str, Field(description="Component address, e.g. starlet.pip or ot2.right.")]
Targets = Annotated[list[str], Field(description="'labware', 'labware:A1', 'labware:A1:H1', 'labware:col2' (A2-H2), 'labware:rowB' (B1-B12) or an alias; one per mounted tip, or one shared container. Columns 2 and 3 of an 8-channel = plate:col2 then plate:col3, one pass each. A head whose channels move together (an 8-channel mount) serves one full column per call, top to bottom; a single-channel mount serves one well per call.")]
Volumes = Annotated[float | list[float], Field(description="uL per target; one number is broadcast. Limits: describe_device / get_params.")]
FlowRate = Annotated[float | None, Field(description="uL/s; omitted = device default (unless a task limit applies).")]
Height = Annotated[float | None, Field(description="mm above the cavity bottom; omitted = device default (unless a task limit applies).")]
Specialized = Annotated[dict[str, Any] | None, Field(description="Device-specific parameters from get_params(device, op); unknown keys are refused.")]
DeviceList = Annotated[list[dict[str, Any]], Field(description="The person's choice, e.g. [{'id': 'starlet', 'model': 'hamilton.starlet', 'backend': 'sim'}]; one device for now.")]
Plan = Annotated[list[str] | None, Field(description="Your plan as short concrete steps; replaces the previous plan. Omit to keep it.")]
PlanStep = Annotated[int, Field(description="The step of your reported plan this call belongs to (1-based); report the plan first with report_decision.")]
CameraList = Annotated[list[dict[str, Any]] | None, Field(description="Optional: the task's cameras, [{'name': 'top', 'device': 'USB Camera'}]; device is the camera's exact name on this computer (macOS AVFoundation). Omitted = keep the config's cameras; [] = none. A camera that keeps its name and device keeps its slot map (map_camera writes one).")]
AutoVerifyArg = Annotated[dict[str, Any] | None, Field(description="Optional, the person's choice: {'checks': ['deck_matches_layout', 'liquid_present'], 'min_streak': 5}. Lets the cameras answer those checks, judged by you from images, once your image judgments agreed with the person's that many times in a row (default 5). Only deck_matches_layout, liquid_present and tips_mounted are allowed. Omitted = keep the config's; {} = off. Part of what the person confirms.")]
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
    self._unwritable: str | None = None  # the held stop whose write-back into the stop file failed
    self.current_action: asyncio.Task | None = None  # what the emergency stop interrupts
    self.current_call: tuple[str, dict[str, Any]] | None = None  # that action's tool and arguments
    self._halting: asyncio.Task | None = None  # the emergency stop reaching the device itself
    self._server = uuid.uuid4().hex[:6]  # tells this server's stop acknowledgement from other servers' (also in one process)
    self._started = time.strftime("%Y-%m-%dT%H:%M:%S")
    # The tips of the run the device holds, by component (a count, or "unknown"): what the robot forgets once
    # that run stops. On real hardware .ahc/tips-left.json follows them, so a kill or a crash cannot lose them.
    self.tips_on: dict[str, int | str] = {}
    self._tips_why: str | None = None  # the stop or unknown outcome that put them in doubt
    self._run_id: str | None = None  # the run the loaded layout started: the robot's id, or one made up here
    self._tips_cleared: str | None = None  # the doubt the person cleared by confirming the pipettes are empty
    self.plan = PlanProgress()  # the agent's plan; fed by the run log entries this server records
    self.step_context: int | None = None  # the plan step of the call running now
    self._bound: str | None = None  # the bound device entry (id, model, backend, where), as a key
    self._limits: str | None = None  # the task limits the bound spec carries
    self._recheck = False  # the config left 'confirmed' while bound: check the deck again once it is back
    self._deferred: list[dict[str, Any]] = []  # entries waiting for this session's first run-log write
    self._identified: str | None = None  # the bound device whose identity the run log holds
    self.interrupted: dict[str, Any] | None = None  # a real device's call the emergency stop cut short
    self._go_ahead: str | None = None  # the config (its digest) the person confirmed in this server session
    self._go_ahead_waiting = False  # the run log holds a `wait` marker: the go-ahead is still awaited
    self._call_began: tuple[datetime, float] | None = None  # the running tool call: wall clock and monotonic start
    self._ran = False  # this folder has run an experiment (found once, never lost)
    self._ops = 0  # calls recorded so far: a refusal nothing below recorded is recorded by act()
    self._audited: str | None = None
    self.disconnected: dict[str, Any] | None = None  # the agent let go of the device: nothing moves until a go-ahead and a load
    self._robot_lock: RobotLock | None = None  # the exclusive hold on the real robot, from load_layout until it is let go
    self._searched: tuple[float, str, dict[str, Any] | None] | None = None  # the last search by name: when, for whom, what
    self._shutting: asyncio.Future | None = None  # the shutdown running now (a signal and the lifespan end share it)
    self._ending: asyncio.Future | None = None  # the signal that is ending this server
    self.handle_signals = False  # main() sets it for the stdio server: SIGTERM and SIGINT end the device's run first
    self._exit: Callable[[int], Any] = os._exit  # how a signalled server ends; tests replace it
    self.dashboard: dict[str, Any] | None = None  # the run dashboard open_dashboard serves or found: url, httpd (ours) or None
    self.view_dashboard: dict[str, Any] | None = None  # the same for the view-only one (no stop or release), for an audience
    self.last_motion = 0.0  # when a call that moves the robot last started or ended (time.time()): older captures show an older deck
    self.vision_seen: dict[str, dict[str, Any]] = {}  # camera -> its latest capture's analysis: {t, path, analysis}
    self.vision_points: list[dict[str, str]] = []  # the points the agent declared (report_decision vision_points)
    self.vision_result: dict[str, Any] | None = None  # the last physical_world_context, with when
    self.proposals: dict[str, dict[str, Any]] = {}  # check id -> the agent's image judgment (propose_verdict), until the person's verdict
    self.auto_checks: dict[str, dict[str, Any]] = {}  # check id -> what verify asked of an auto check (agent_verdict judges it again)
    self.view = None
    if view:
      from ahc.viz.viewer import LiveView  # optional: pulls in the 3D viewer's web servers
      self.view = LiveView("AHC")

  async def run(self, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    async with self.lock:
      self._call_began = (datetime.now(), time.monotonic())  # a call's time counts from its turn, not its queueing
      try:
        return await fn()
      finally:
        self._call_began = None

  # -- emergency stop -------------------------------------------------------------------------

  def poll_estop(self) -> None:
    """Read .ahc/estop.json when it changed: latch an engage, clear only on its release record.

    The device's own stop is scheduled before anything is written, so a run log that cannot be written
    never keeps the stop from the robot, and before the action is cancelled, so it still finds a run the
    action is setting up. Every entry carries the stop's id. While a stop is held, a file that no longer
    says so (deleted, or a release record of another stop) gets the held stop written back, so the file,
    ahc-estop --status, the dashboard and the hooks all say stopped, as the server does.
    """
    try:
      mtime = estop.path(self.ws).stat().st_mtime
    except OSError:
      if self.estop is not None:  # deleted while engaged: still stopped, so put the record back
        self._write_back("estop_file_removed", "the emergency stop file was deleted while engaged; the server wrote it back")
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
      if self.adapter is not None:
        try:  # not under self.lock: the action it stops may hold it
          self._halting = asyncio.get_running_loop().create_task(self._halt_device(self.adapter))
        except RuntimeError:  # no event loop (a synchronous caller): the server's watcher halts it instead
          pass
      if self.adapter is None:
        self._acknowledge(data, "no_device")
      elif self._halting is None:
        self._acknowledge(data, "not_confirmed", self.adapter, detail="no event loop to send the device its stop")
      else:
        self._acknowledge(data, "pending", self.adapter)
      # Only now the action: both resume in the order they were scheduled, so the halt's first step finds
      # the run a load is still setting up (homing) before the cancelled load lets go of it.
      if interrupted:
        self.current_action.cancel()
      call = self.current_call if interrupted else None
      self._tips_event(f"the emergency stop at {str(data.get('at') or '')[11:16] or 'an unknown time'}"
                       + (f" during {call[0]}" if call else ""), call)
      self._note({"type": "estop", "state": "engaged", "id": data.get("id"), "by": data.get("by"), "at": data.get("at"),
                  "reason": data.get("reason"), "interrupted": interrupted})
    elif not data.get("engaged") and self.estop is not None and self.estop.get("id") == data.get("id"):
      self.estop = None
      self._note({"type": "estop", "state": "released", "id": data.get("id"), "by": data.get("released_by"),
                  "at": data.get("released_at")})
      if self.gate is not None:  # the device stopped mid-run: the deck has to be checked again
        self.gate = self.gate.renewed()
        self._note({"type": "gate", **self.gate.public(), "reason": "emergency stop released: check the deck again"})
    elif not data.get("engaged") and self.estop is not None:
      # A release record of another stop (written by hand, or a press and a release that both landed between
      # two polls) releases nothing here: the held stop goes back into the file, and the person releases again.
      other = f"stop {data['id']}" if data.get("id") else "a stop without an id"
      self._write_back("estop_release_mismatch",
                       f"the stop file held a release record of {other}, but this server holds stop "
                       f"{self.estop.get('id')}; the server wrote its stop back: the person releases it once more",
                       released_id=data.get("id"))

  def _write_back(self, event: str, detail: str, **ids: Any) -> None:
    """The held stop back into the stop file (atomically, as ahc-estop writes it), so every reader says stopped
    again; recorded either way. A file that cannot be written leaves the server stopped all the same."""
    try:
      estop._write(self.ws, self.estop)
      self._unwritable = None
    except OSError as exc:
      if self._unwritable == self.estop.get("id"):
        return  # recorded once: a deleted file is found again at every poll
      self._unwritable = self.estop.get("id")
      detail += f"; writing it back failed ({exc}), so the file may not say stopped, but the server is"
    self._note({"type": "audit", "event": event, "id": self.estop.get("id"), **ids, "detail": detail})

  def _note(self, entry: dict[str, Any]) -> None:
    """A record of the emergency stop; it never raises into the stop. A server without a run log yet (another
    session open on this folder, one that only read) holds the stop in memory and writes nothing: nothing
    of its own can be moving, since every motion comes after a reported plan, which starts the log."""
    if not self.log_started():
      return
    try:
      self.record(entry)
    except Exception as exc:  # noqa: BLE001
      print(f"ahc: not recorded ({type(exc).__name__}: {exc}): {json.dumps(entry, default=str)}", file=sys.stderr)

  def _acknowledge(self, stop: dict[str, Any], halt: str, adapter=None, run_active: bool | None = None,
                   detail: str | None = None) -> None:
    """This server's own acknowledgement of the stop (estop.py): the device bound, whether a run was active, what the
    device's stop did. Never raises into the stop."""
    device = None if adapter is None else {"id": adapter.spec.id, "model": adapter.spec.model, "backend": adapter.backend,
                                           "robot": adapter.options.get("robot_name")}
    try:
      estop.write_ack(self.ws, stop, self._server, self._started,
                      {"device": device, "run_active": run_active, "halt": halt, "detail": detail})
    except Exception as exc:  # noqa: BLE001
      print(f"ahc: stop acknowledgement not written ({type(exc).__name__}: {exc})", file=sys.stderr)

  async def _halt_device(self, adapter) -> None:
    """The emergency stop reaching the device: the OT-2's run is stopped; other devices have no stop
    this layer can reach yet. Logged either way."""
    stop = self.estop
    try:
      halted = await adapter.halt()
    except Exception as exc:  # noqa: BLE001 - the stop must be reported, never raised into the watcher
      if stop is not None:
        other = getattr(exc, "code", None) == "other_run_active"
        self._acknowledge(stop, "other_run_active" if other else "not_confirmed", adapter, run_active=True,
                          detail=getattr(exc, "message", None) or f"{type(exc).__name__}: {str(exc).rstrip('.')}")
      self._note({"type": "audit", "event": "device_halt_failed",
                  "detail": f"{type(exc).__name__}: {str(exc).rstrip('.')}. The robot may still be executing its last "
                            "command; the power switch is the immediate stop."})
      self._save_deck()
      return
    if stop is not None:
      none_to_stop = type(adapter).halt is Adapter.halt  # the base class: this device has no stop to reach
      self._acknowledge(stop, "stopped" if halted else "no_stop" if none_to_stop else "no_run", adapter,
                        run_active=True if halted else None if none_to_stop else False,
                        detail="the robot accepted the stop and its run reports stopped; the command in flight may have "
                               "finished first" if halted else "no device-level stop for this device" if none_to_stop
                        else "nothing was running on the device")
    if halted:
      self._note({"type": "audit", "event": "device_halted",
                  "detail": "the robot accepted the stop and its run reports stopped; the command in flight may have "
                            "finished first"})
    else:
      self._note({"type": "audit", "event": "device_not_halted",
                  "detail": "no device-level stop for this device, or nothing was running on it"})
    self._save_deck()

  async def watch_estop(self) -> None:
    """Runs for the server's life; a stat every 0.2 s, parsing only when the file changes."""
    while True:
      try:
        self.poll_estop()
      except Exception:  # noqa: BLE001 - the watcher must outlive a failed poll; every tool call polls again
        pass
      await asyncio.sleep(0.2)

  def require_not_stopped(self) -> None:
    self.poll_estop()
    if self.estop is not None:
      raise LabError("emergency_stop",
                     f"the emergency stop was pressed at {(self.estop.get('at') or '').replace('T', ' ')} "
                     f"({self.estop.get('by')}); nothing runs until the person releases it.",
                     "Stop now and tell the person. Do not retry, work around it or release it yourself.")

  async def open_dashboard(self, open_browser: bool, view_only: bool = False) -> dict[str, Any]:
    """This task folder's run dashboard, served from this process (it ends with the server): started on the first
    free port from 8770, or this folder's own dashboard that is already running. Asked again, the same one.
    view_only: the page without stop or release, a second dashboard on a port of its own next to the control one."""
    def start() -> tuple[Any, str]:  # blocking: binds a port and may probe one
      return run_dashboard.open_dashboard(self.ws, lambda: self.runlog.path if self.runlog else None,
                                          DASHBOARD_PORT, open_browser=False, view_only=view_only)
    shown = self.view_dashboard if view_only else self.dashboard
    if shown is not None and shown["httpd"] is None:  # another process's: still this folder's, still there?
      port = int(shown["url"].rsplit(":", 1)[1].strip("/"))
      other = await asyncio.to_thread(run_dashboard.whoami, port)
      if other is None or not run_dashboard._same_folder(other.get("folder"), self.ws.root) \
          or bool(other.get("view_only")) != view_only:
        shown = None
    started = shown is None
    if started:
      try:
        httpd, where = await asyncio.to_thread(start)
      except OSError as exc:
        raise LabError("dashboard_unavailable", f"the dashboard could not start: {exc}",
                       "The person can run the dashboard command lab_overview.emergency_stop.how names.") from exc
      shown = {"url": where.split(" ", 1)[0], "httpd": httpd}
      started = httpd is not None  # None: this folder's own dashboard was already running
    if view_only:
      self.view_dashboard = shown
    else:
      self.dashboard = shown
    opened = False
    if open_browser:
      try:  # the person's browser; a machine with none must not fail the tool
        opened = bool(await asyncio.to_thread(webbrowser.open, shown["url"]))
      except Exception:  # noqa: BLE001
        opened = False
    return {"url": shown["url"], "started": started, "browser_opened": opened,
            **({"view_only": True, "note": "no stop or release on this page; the person keeps the control dashboard"}
               if view_only else {"how_to_stop": "the stop button on this page"})}

  def stop_dashboard(self) -> None:
    """Ends the dashboard this server serves (one another process serves is left alone)."""
    for shown in (self.dashboard, self.view_dashboard):
      httpd = shown["httpd"] if shown else None
      if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
        httpd.thread.join(timeout=5)
    self.dashboard = self.view_dashboard = None

  async def shutdown(self) -> None:
    """Ends the device's run, frees the robot and closes the dashboard. A signal and the lifespan end may both ask:
    asked while it runs, they share the one shutdown; asked again once it is done, there is nothing left to end."""
    if self._shutting is None:
      self._shutting = asyncio.ensure_future(self._shut_down())
    try:
      await asyncio.shield(self._shutting)
    finally:
      if self._shutting is not None and self._shutting.done():
        self._shutting = None

  async def _shut_down(self) -> None:
    async def device() -> None:
      async with self.lock:
        await self._unbind("the server shut down")
        if self.view is not None:
          await self.view.stop()
    # Together: a call in flight that holds the lock must not keep the dashboard open (or the other way round).
    await asyncio.gather(device(), asyncio.to_thread(self.stop_dashboard))

  def install_signal_handlers(self) -> None:
    """SIGTERM and SIGINT end this server the way its lifespan ends: the device's run stopped, the dashboard closed."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
      try:
        loop.add_signal_handler(sig, self._signalled, sig)
      except (NotImplementedError, RuntimeError, ValueError):  # not the main thread, or a platform without them
        pass

  def _signalled(self, sig: int) -> None:
    if self._ending is None:  # a second signal while it ends (SIGINT, then SIGTERM) changes nothing
      self._ending = asyncio.ensure_future(self.end_on_signal(sig))

  async def end_on_signal(self, sig: int) -> None:
    """The shutdown, bounded by SIGNAL_SHUTDOWN_S; then the process exits whether or not the robot confirmed, and the
    run log says which. Claude Code's own exit and a plain `kill` end a server this way."""
    name = signal.Signals(sig).name
    self._note({"type": "audit", "event": "signal_shutdown", "signal": name,
                "detail": f"{name} received: ending the device's run and closing the dashboard, then exiting"})
    try:
      await asyncio.wait_for(self.shutdown(), SIGNAL_SHUTDOWN_S)
      failure = None
    except asyncio.TimeoutError:
      failure = f"it was not done within {SIGNAL_SHUTDOWN_S:g} s"
    except Exception as exc:  # noqa: BLE001 - the process ends all the same
      failure = f"{type(exc).__name__}: {exc}"
    if failure is not None:
      detail = (f"{name}: the device's stop was not confirmed ({failure}); the server exits anyway. The robot's run may "
                "still be open (a robot that is off or unreachable); a tips note, if tips were on, stays for the next "
                "load_layout.")
      print(f"ahc: {detail}", file=sys.stderr)
      self._note({"type": "audit", "event": "signal_shutdown_unconfirmed", "signal": name, "detail": detail})
    for stream in (sys.stdout, sys.stderr):
      try:
        stream.flush()
      except Exception:  # noqa: BLE001
        pass
    self._exit(0 if failure is None else 1)

  # -- workspace config -----------------------------------------------------------------------

  async def refresh(self, write: bool) -> None:
    """Read the workspace config again (create it on the first write) and bind its device once confirmed.

    A config that is not usable (pending, invalid, gone) leaves the bound device as it is, with its
    run, deck and mounted tips: motion is refused until the config is confirmed again, and then the
    deck is checked again. Only a confirmed change of the device entry rebinds; reading never does.
    """
    text = self.ws.read_config_text()
    if text is None and write:
      self._create_config()
      text = self.ws.read_config_text()
    try:
      if text is None and not self.forced_sim:
        self.config, self.problem, self.on_disk = None, None, False
        self._unconfirmed()
        return
      config = self._simulation_config() if text is None else parse_config(text)
      self._check_device_flag(config)
      for entry in config.devices:
        check_entry(entry)
    except LabError as exc:
      self.config, self.problem = None, exc
      self._unconfirmed()
      return
    self.config, self.problem, self.on_disk = config, None, text is not None
    if config.confirmation and config.confirmation.get("digest") != config.digest():
      self._audit_outside_edit(config)
    if config.status(self.forced_sim)[0] != "confirmed":
      self._unconfirmed()
      return
    try:
      await self._bind(config)
    except LabError as exc:  # e.g. task limits that would loosen the description file's
      self.problem = exc
      self._unconfirmed()
    except Exception as exc:  # noqa: BLE001 - a device that cannot be set up must not break every tool
      self.problem = LabError("device_refused", f"the device could not be set up: {type(exc).__name__}: {exc}",
                              "Tell the person; nothing moves until the device and its config work again.")
      self._unconfirmed()

  def _unconfirmed(self) -> None:
    """The config is not usable now. A bound device stays bound (motion is refused meanwhile); the deck
    is checked again once the config is confirmed again."""
    if self.adapter is not None:
      self._recheck = True

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
      self.ws.write_config_text(template_text())

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
                   "detail": "devices or limits differ from what was confirmed; the config is pending again"},
                  defer=True)

  async def _bind(self, config: Config) -> None:
    """Bind the config's device. The same device entry keeps its adapter (its run, deck and mounted
    tips): new task limits only change the checks. A changed entry (model, backend, where) rebinds."""
    entry = config.devices[0]
    check_entry(entry)
    backend = "sim" if self.forced_sim else entry.backend
    options = self._endpoint(entry, backend)
    # The mounts too: a simulator's endpoint options do not carry them, but they decide the spec.
    key = json.dumps({"id": entry.id, "model": entry.model, "backend": backend, "mounts": entry.options.get("mounts"),
                      "options": {k: v for k, v in options.items() if not callable(v)}}, sort_keys=True, default=str)
    limits = json.dumps(config.limits, sort_keys=True, default=str)
    if self.adapter is not None and self._bound == key:
      if limits != self._limits:
        spec = apply_limits(replace(device_spec(entry.model, entry.options), id=entry.id), config.limits)
        self.spec = self.adapter.spec = self.lh.spec = self.verification.spec = spec
        self._limits = limits
      if self._recheck:
        self._recheck = False
        if self.gate is not None:  # back from pending: the deck is checked again before anything moves
          self.gate = self.gate.renewed()
          self.gate_changed("the config was confirmed again: check the deck again", defer=True)
      return
    spec = apply_limits(replace(device_spec(entry.model, entry.options), id=entry.id), config.limits)
    adapter = ADAPTERS[spec.model](spec, backend, options)
    await self._unbind("the device config changed")
    set_tip_tracking(True)
    set_volume_tracking(True)
    await adapter.start()
    if self.runlog is not None and self.runlog.device not in (None, spec.model):
      self.runlog = None  # one device per run log
    elif self.runlog is not None:
      self.runlog.device, self.runlog.backend = spec.model, backend  # its header if not written yet; its summary
    self.spec, self.adapter, self._bound, self._limits = spec, adapter, key, limits
    self._identified = None
    self.lh = LiquidHandling(spec, adapter, self)
    self.verification = Verification(spec, adapter, self, unknown=self.unknown_state, tips_note=self.tips_note)

  def _endpoint(self, entry: DeviceEntry, backend: str) -> dict[str, Any]:
    """The options the adapter runs with.

    A real backend: the entry the person confirmed, nothing else. A server-level host or port
    (--host/--port, AHC_OT2_HOST/AHC_OT2_PORT) that differs from it is refused, never applied.
    Simulation: this task's own simulator, or one named at server level on this machine; never the
    entry's address, so forcing simulation can never reach the robot a confirmed config names.
    """
    overrides = {k: self.options[k] for k in ("host", "port") if k in self.options}
    if backend != "sim":
      confirmed = {"host": entry.options.get("host"), "port": entry.options.get("port") or DEFAULT_PORT}
      differ = {k: v for k, v in overrides.items() if str(v).strip().lower() != str(confirmed[k]).strip().lower()}
      if differ:
        given = ", ".join(f"{k} {v}" for k, v in differ.items())
        raise LabError("endpoint_override",
                       f"this server was started with {given} (--host/--port or AHC_OT2_HOST/AHC_OT2_PORT), but the "
                       f"person confirmed the robot at {confirmed['host']}:{confirmed['port']}; a real robot is driven "
                       "only at its confirmed address.",
                       "Tell the person: start the server without --host/--port and without AHC_OT2_HOST/AHC_OT2_PORT "
                       "(they are for simulators). Another robot is written with configure_devices and confirmed by the person.")
      return dict(entry.options)
    sim = SIMULATORS.get(entry.model)
    if sim is None:  # simulated in this process (the STARlet): nothing to reach
      return {}
    host = overrides.get("host")
    if host is not None and not _loopback(str(host)):
      raise LabError("sim_endpoint_not_local",
                     f"the server-level host {host!r} is not this machine; a simulator runs on this machine only, "
                     "and simulation never drives a robot.",
                     "Tell the person: drop --host / AHC_OT2_HOST, or give 127.0.0.1.")
    ws = self.ws
    options: dict[str, Any] = {**sim.workspace_options(ws), **overrides,
                               "diagnose_unreachable": lambda h, p, exc: sim.diagnose(ws, h, p, exc)}
    if "port" in overrides:  # a simulator started elsewhere (tests, ahc-demo): an AHC simulator, never a robot
      def unreachable(h, p, exc) -> LabError:
        """Nothing answers at the port named at server level, and this server stays on that port: this task's
        own simulator, on a port of its own, would not reach it."""
        pinned = (f"This server stays on port {p}: it was started with --port or AHC_OT2_PORT (or created with that "
                  "port). Have it restarted without --port and with AHC_OT2_PORT unset to use this task's own simulator")
        found = sim.diagnose(ws, h, p, exc)
        if found.code != "sim_not_running":  # not installed: that comes first
          return LabError(found.code, found.message, f"{found.hint.rstrip('.')}. {pinned}.")
        return LabError("sim_not_running", f"no OT-2 simulator answers at {h}:{p}, the port this server was started "
                        f"with ({exc}).", f"Start the simulator that serves port {p} again. {pinned}, started in a "
                        f"background shell with {sim.start_command(ws)}.")

      def check_dev(h, p):
        try:
          sim.check_dev_simulator(h, p)
        except LabError as exc:
          if exc.code != "sim_not_running":
            raise
          raise unreachable(h, p, "nothing answers") from None
      options.update(check_endpoint=check_dev, diagnose_unreachable=unreachable)
    else:  # only this task's own instance
      options["check_endpoint"] = lambda h, p: sim.check_endpoint(ws, h, p)
    return options

  async def _unbind(self, why: str) -> bool:
    """Drop the device, best effort: one that cannot be stopped (the robot off or rebooted) must not
    wedge the server, so a failed stop is recorded and the device dropped anyway. Then the robot is free for another
    server (the lock goes after the stop, never before). False: the stop failed."""
    adapter = self.adapter
    stopped = True
    if adapter is not None:
      self._mark_tips_left(adapter, why)
      try:
        await adapter.stop()
      except Exception as exc:  # noqa: BLE001
        stopped = False
        self.record({"type": "audit", "event": "device_stop_failed",
                     "detail": f"{why}; stopping the device failed ({type(exc).__name__}: {exc}). Its last run may "
                               "still be active on the robot; it is dropped here anyway."}, defer=True)
    self.spec = self.adapter = self.lh = self.verification = self.gate = None
    self._bound = self._limits = None
    self._recheck = False
    self.tips_on, self._tips_why, self._run_id = {}, None, None
    self.release_robot()
    return stopped

  def _mark_tips_left(self, adapter, why: str) -> None:
    """A real device dropped (the server shut down, the config changed) with tips on or in doubt: its run stops
    and the robot forgets them. The note says so until the person confirms the pipettes are empty; a stop or an
    unknown outcome before keeps its own reason, and so does what the person found. A run in doubt that nobody saw
    through is noted even with no tips known: only the person can say none are on."""
    if adapter.simulated:
      return
    doubt = self._doubt_key()
    if self._tips_noted():
      self._write_tips(why)
    elif doubt is not None and doubt != self._tips_cleared:
      self._write_tips(f"{self._run_doubt()}, then {why}")

  # -- the robot: one server at a time, let go of on request, found by its name ------------------------

  def claim_robot(self) -> None:
    """A real robot is driven by at most one server (one session) at a time: load_layout, which starts a run, takes an
    exclusive lock keyed by the robot's name under AHC_HOME (robot_lock.py). It stays until the device is let go of:
    disconnect, the server ending, a new config. Simulators are per task already: no lock."""
    adapter = self.adapter
    if adapter.simulated or self._robot_lock is not None:
      return
    host, port = getattr(adapter, "host", None), getattr(adapter, "port", None)
    name = adapter.options.get("robot_name") or (f"{host}:{port}" if host else self.spec.model)
    lock = RobotLock(str(name))
    who = {"workspace": str(self.ws.root), "session_started": self._started, "claude_session": claude_session(),
           "endpoint": f"{host}:{port}" if host else None}
    try:
      holder = lock.acquire(who)
    except OSError as exc:  # the lock is a guard against two sessions, not a condition for one
      print(f"ahc: the robot lock {lock.path} could not be taken ({exc}); going on without it", file=sys.stderr)
      self._note({"type": "audit", "event": "robot_lock_unavailable", "detail": f"{lock.path}: {exc}"})
      return
    if holder is not None:
      seen = (f"pid {holder.get('pid', 'unknown')}, task folder {holder.get('workspace', 'unknown')}, session started "
              f"{str(holder.get('session_started') or 'at an unknown time').replace('T', ' ')}"
              + (f", Claude Code session {holder['claude_session']}" if holder.get("claude_session") else ""))
      raise LabError("robot_in_use", f"the robot {str(name)!r} is driven by another AHC server ({seen}); a robot has one "
                     "server at a time.",
                     "Tell the person: that session must disconnect (or end) before this one can load a layout; the "
                     "person decides which session drives the robot. Do not retry in a loop or work around it.")
    self._robot_lock = lock

  def release_robot(self) -> None:
    lock, self._robot_lock = self._robot_lock, None
    if lock is not None:
      lock.release()

  async def disconnect(self) -> dict[str, Any]:
    """Let go of the device without an emergency stop: its run ends (a robot is asked whether it did), the transport
    closes and the robot is free for another server; this server, the dashboard and the run log stay. Tips still on
    a pipette are noted as a restart notes them. Nothing moves afterwards until the person's go-ahead and a load."""
    self.require_not_stopped()
    await self.refresh(write=False)
    self.require_device()
    if self.disconnected is not None:
      return {"disconnected": True, "run_ended_confirmed": None, "tips_left": {},
              "next": "Already disconnected at " + str(self.disconnected["at"]).replace("T", " ") + ". " + self.next_step()}
    adapter = self.adapter
    run, real, tips = adapter.run_id(), not adapter.simulated, dict(self._tips_noted())
    robot = adapter.options.get("robot_name")
    stopped = await self._unbind("the agent disconnected the device")
    ended = await adapter.run_ended(run) if run is not None else None  # asked: stop() returning is not the robot's word
    self._go_ahead = None
    self.disconnected = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "robot": robot}
    if ended is True:
      first = "The robot reports its run ended, and this server let go of it." if real else "The simulated run ended."
    elif ended is False:
      first = ("The robot has NOT confirmed that its run ended" + ("" if stopped else " (the stop failed)") + ": it may "
               "still be executing its last command. Tell the person; the Opentrons App or the power switch ends it.")
    else:
      first = "No run was open on the device."
    leftover = (f" Tips may still be on the pipettes ({_tips_count(tips)}): the person removes them by hand; load_layout then "
                "waits for their pass on verify(check='tips_mounted', expect='none')." if real and tips else "")
    return {"disconnected": True, "run_ended_confirmed": ended, "tips_left": tips,
            "next": f"{first} The server, the dashboard and the run log stay up.{leftover} To drive again: the person's "
                    "go-ahead (confirm_config(summary=config.confirm_summary)), then load_layout; lab_overview has the details."}

  def require_connected(self, op: str) -> None:
    """After disconnect only load_layout runs, and that only with the person's go-ahead (require_go_ahead)."""
    if self.disconnected is None or op == "load_layout":
      return
    raise LabError("disconnected", f"the device was disconnected at {str(self.disconnected['at']).replace('T', ' ')} "
                   "(disconnect), so nothing moves until the person gives their go-ahead again and a layout is loaded.",
                   f"{self._go_ahead_step()}Then load_layout; the deck is checked before anything moves.")

  async def renamed(self, exc: LabError) -> LabError:
    """A robot that does not answer at its address (backend_unreachable), with where a robot of its configured name
    answers instead, if one does: its address may change between days (USB link-local). The address is never switched
    here; the person confirms the new one. Never in simulation."""
    if exc.code != "backend_unreachable":
      return exc
    found = await self.search_by_name()
    if found is None:
      return exc
    port = "" if found.get("port") in (None, DEFAULT_PORT) else f" and options.port {found['port']}"
    return LabError(exc.code, f"{exc.message.rstrip('.')}. A robot named {self.adapter.options.get('robot_name')!r} answers at "
                    f"{found['host']}:{found.get('port')}.",
                    f"configure_devices with options.host {found['host']}{port}; the person confirms (the name is checked). "
                    "Never switch the address without them.")

  async def search_by_name(self) -> dict[str, Any] | None:
    """The robot of the configured name, answering at another address than the entry's, from a short find_robots
    search (read-only; bounded; kept for a while). None: none found, nothing to look for, or simulating."""
    adapter = self.adapter
    name = adapter.options.get("robot_name") if adapter is not None else None
    if adapter is None or adapter.simulated or self.forced_sim or not name:
      return None
    if self._searched is not None and self._searched[1] == name and time.monotonic() - self._searched[0] < ROBOT_SEARCH_KEPT_S:
      return self._searched[2]
    try:
      robots = await asyncio.wait_for(asyncio.to_thread(search_robots, ROBOT_SEARCH_S), ROBOT_SEARCH_BOUND_S)
    except (asyncio.TimeoutError, LabError, OSError):
      return None
    here = (adapter.host, adapter.port)
    found = next((r for r in robots if r.get("name") == name and (r.get("host"), r.get("port")) != here), None)
    self._searched = (time.monotonic(), name, found)
    return found

  # -- tips a real robot may carry ----------------------------------------------------------------

  def _pipettes(self) -> dict[str, Any]:
    """The bound device's liquid-handling components, by address."""
    return {f"{self.spec.prefix}.{c.name}": c for c in self.spec.components.values()
            if "liquid_handling" in c.capabilities}

  def _address(self, device: Any) -> str | None:
    """The pipette a call names, by its full address ('right' and 'ot2.right' alike), or None when it names none."""
    if self.spec is None or not isinstance(device, str):
      return None
    try:
      address = f"{self.spec.prefix}.{self.spec.component(device).name}"
    except LabError:
      return None
    return address if address in self._pipettes() else None

  def live_run(self) -> str | None:
    """The run the device holds now (also one the emergency stop halted), or None: what its tips belong to."""
    return self._run_id if self.adapter is not None and self.adapter.layout is not None else None

  def started_run(self, previous: str | None) -> None:
    """load_layout started a new run: no tips on, and the old run's stop or unknown outcome is behind it. A note
    of the old run that the checks before the load let through held nothing any more (a drop whose clearing
    failed): it goes too."""
    self._run_id = self.adapter.run_id() or uuid.uuid4().hex[:12]
    self.tips_on, self._tips_why = {}, None
    self.disconnected = None  # connected again: the person's go-ahead came before this load
    note = self.ws.tips_left()
    if previous is not None and note is not None and note.get("run") == previous:
      try:
        self.ws.clear_tips_left()
      except OSError:
        pass

  def _tips_noted(self) -> dict[str, int | str]:
    """What the note holds: the live run's tips, and each pipette the person found tips on (unknown: the robot may
    not know them)."""
    found = self.verification.tips_found() if self.verification is not None else {}
    return {**{address: "unknown" for address in found}, **self.tips_on}

  def _found_why(self) -> str | None:
    found = self.verification.tips_found() if self.verification is not None else {}
    return "; ".join(dict.fromkeys(found.values())) or None

  def _write_tips(self, why: str, mounted: dict[str, int | str] | None = None) -> bool:
    """The note of the tips the live run holds or may hold (`mounted`, else _tips_noted), .ahc/tips-left.json; it never
    raises. A stop or an unknown outcome keeps its own why, then what the person found. The note of another run (or one
    that cannot be read) stays as it is: it holds the next run back already, and the person's check of every pipette
    that clears it covers these tips too. Returns whether this note is the one on disk now."""
    other = self.ws.tips_left()
    if other is not None and (other.get("run") is None or other.get("run") != self.live_run()):
      return False
    robot = self.adapter.options.get("robot_name")
    note = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "why": self._tips_why or self._found_why() or why,
            "device": self.spec.model, **({"robot": robot} if robot else {}), "run": self.live_run(),
            "mounted": dict(self._tips_noted() if mounted is None else mounted), **self.endpoint()}
    if other is not None and {**other, "at": note["at"]} == note:
      return True  # it says so already, since the time it gives
    try:
      self.ws.mark_tips_left(note)
    except OSError as exc:
      print(f"ahc: tips may be left on the pipettes ({note}); the note could not be written: {exc}", file=sys.stderr)
      return False
    return True

  def _note_tips(self, why: str) -> None:
    """Real hardware: the note follows the tips the live run holds and those the person found, so it outlives this
    server (a kill or a crash too); once none may be on any pipette, this run's note goes."""
    if self._tips_noted():
      self._write_tips(why)
      return
    note, live = self.ws.tips_left(), self.live_run()
    if live is not None and note is not None and note.get("run") == live:
      try:
        self.ws.clear_tips_left()
      except OSError as exc:
        print(f"ahc: the tips note {self.ws.tips_left_path} could not be removed: {exc}", file=sys.stderr)

  def tips_checked(self) -> None:
    """The person's verdict on a tips check on real hardware: a pipette they found tips on (a check that asked for
    none, which they failed) is noted, so neither a restart nor a kill loses it; a drop on it that goes through, or a
    later check of it that passes, takes it off."""
    self._note_tips("picked up")

  def _picking_up(self, device: Any) -> tuple[dict[str, Any] | None] | None:
    """Real hardware, before a pick-up is sent: its pipette is noted as unknown, so a server that ends while the robot
    picks up (killed, crashed, or its client gone) still leaves the note. Returns the note it replaced, put back if
    the pick-up is refused before anything moved; None when nothing was written."""
    address = self._address(device)
    if address is None or self.adapter.simulated or self._doubt_key() is not None:
      return None  # a stop or an unknown outcome stands: the pick-up is refused before anything is sent
    previous = self.ws.tips_left()
    return (previous,) if self._write_tips("picking up", {**self.tips_on, address: "unknown"}) else None

  def _put_back(self, noted: tuple[dict[str, Any] | None]) -> None:
    """A pick-up refused before anything was sent: the note as it was before it (none, or the run's tips)."""
    if self._doubt_key() is not None:  # a stop since: its own note stands
      return
    try:
      if noted[0] is None:
        self.ws.clear_tips_left()
      else:
        self.ws.mark_tips_left(noted[0])
    except OSError as exc:
      print(f"ahc: the tips note {self.ws.tips_left_path} could not be put back: {exc}", file=sys.stderr)

  def _tips_changed(self) -> None:
    """A pick-up or a drop went through: the tips as the device holds them now. On real hardware the note follows
    them, so it outlives this server (a kill or a crash too); a drop that leaves none on any pipette clears it."""
    mounted: dict[str, int | str] = {}
    for address, comp in self._pipettes().items():
      try:
        count: int | str = len(self.adapter.mounted_tips(comp))
      except Exception:  # noqa: BLE001 - the device cannot say: as good as unknown
        count = "unknown"
      if count:
        mounted[address] = count
    self.tips_on = mounted
    if not self.adapter.simulated:
      self._note_tips("picked up")

  def _tips_event(self, why: str, call: tuple[str, dict[str, Any]] | None) -> None:
    """A stop or an unknown outcome on a real device: the robot may no longer know the tips its run holds (it
    forgets them once the run stops), and a call that picks up or drops tips leaves that pipette unknown. Noted at
    once, so neither a restart nor a crash loses it; the first such event of a run keeps its reason. Never raises
    into the stop."""
    try:
      if self.adapter is None or self.adapter.simulated:
        return
      mounted = dict(self.tips_on)
      address = self._address((call[1] or {}).get("device")) if call and call[0] in ("pick_up_tips", "drop_tips") \
        else None
      if address is not None:
        mounted[address] = "unknown"
      self.tips_on = mounted
      if self._tips_noted():
        self._tips_why = self._tips_why or why
        self._write_tips(self._tips_why)
      self._save_deck()
    except Exception as exc:  # noqa: BLE001
      print(f"ahc: tips not noted after {why} ({type(exc).__name__}: {exc})", file=sys.stderr)

  def _save_deck(self) -> None:
    """The twin's deck as it stands, .ahc/state/deck.json (volumes, tip spots, tips on the pipettes, what is unknown):
    after every call that changes it, so a crash or a stop leaves the last known state. It never raises into a call."""
    try:
      adapter = self.adapter
      if adapter is None or adapter.layout is None or adapter.layout.declared is None:
        return
      doubt = self.unknown_state()
      mounted: dict[str, Any] = {}
      for address, comp in self._pipettes().items():
        try:
          mounted[address] = "unknown" if doubt else len(adapter.mounted_tips(comp))
        except Exception:  # noqa: BLE001 - the device cannot say: as good as unknown
          mounted[address] = "unknown"
      device = {"model": self.spec.model, "id": self.spec.prefix, "backend": adapter.backend}
      self.ws.write_deck(deck_state.snapshot(adapter.layout, run_id=self._run_id, device=device, mounted=mounted,
                                             doubt=doubt))
    except Exception as exc:  # noqa: BLE001
      print(f"ahc: the deck state {self.ws.deck_path} was not written ({type(exc).__name__}: {exc})", file=sys.stderr)

  def _doubt_key(self) -> str | None:
    """The live run's stop or unknown outcome on real hardware, as the key the person's confirmation that the
    pipettes are empty is held against; None while the run knows its tips."""
    adapter = self.adapter
    if adapter is None or adapter.simulated:
      return None
    if not (adapter.halted or adapter.halt_requested or adapter.outcome_unknown or self.interrupted):
      return None
    return json.dumps([self.live_run(), adapter.outcome_unknown, self.interrupted], sort_keys=True, default=str)

  def _run_doubt(self) -> str:
    """Why the live run cannot vouch for its tips (while _doubt_key() is not None)."""
    adapter = self.adapter
    if adapter.outcome_unknown:
      return f"the outcome of {adapter.outcome_unknown.get('op')} on the robot is unknown"
    if self.interrupted:
      return f"the emergency stop interrupted {self.interrupted.get('op')}"
    if adapter.halted:
      return "the emergency stop stopped the robot's run"
    return "the emergency stop was sent to the robot, which has not confirmed that its run stopped"

  def tips_note(self) -> str | None:
    """Real hardware: why tips may be on the pipettes that the robot no longer knows of, or None. A note another
    run left (a server that ended with tips on, a kill or a crash), or the live run's own stop or unknown outcome
    until the person confirms the pipettes are empty. A live run that knows its tips gives none."""
    if self.forced_sim or (self.adapter is not None and self.adapter.simulated):
      return None
    key = self._doubt_key()
    if key is not None and key != self._tips_cleared:
      if self.tips_on:
        return (f"Tips may still be on the pipettes: {_tips_count(self.tips_on)} "
                f"({self._tips_why or self._run_doubt()}), and the robot no longer knows them.")
      doubt = self._run_doubt()
      return (f"{doubt[0].upper()}{doubt[1:]}: no tips were on as far as this server knows, but the robot no longer "
              "keeps track of them.")
    note = self.ws.tips_left()
    live = self.live_run()
    if note is None or (live is not None and note.get("run") == live):
      return None
    if "unreadable" in note:  # it was written for a reason: as good as tips nobody can count
      return (f"Tips may still be on the pipettes: the tips note {self.ws.tips_left_path} exists but cannot be read "
              f"({note['unreadable']}), so how many, and on which pipette, is unknown.")
    why = note.get("why")
    # No shutdown wrote why: that server was killed or crashed, or it is another session still open on this folder.
    if why == "picked up":
      why = ("picked up in a run that never dropped them: its server was killed or crashed, unless another agent "
             "session open on this folder still holds them (then that one drops them first)")
    elif why == "picking up":
      why = ("picked up in a run whose server ended during the pick-up, unless another agent session open on this "
             "folder is still at it")
    robot = self._other_robot(note)
    return (f"Tips may still be on the pipettes{robot}: {_tips_count(note.get('mounted'))} ({why}, "
            f"{str(note.get('at') or 'time unknown').replace('T', ' ')}), and {'that' if robot else 'the'} robot no "
            "longer knows they are there.")

  def _other_robot(self, note: dict[str, Any]) -> str:
    """' of the robot 'name' at host:port' for a note about another robot than the one bound now (or with none bound),
    so the person checks that robot's pipettes; '' for the bound robot's own note, or one that names no robot."""
    name, where = note.get("robot"), note.get("endpoint")
    if self.adapter is not None:
      mine = self.adapter.options.get("robot_name")
      if (name == mine) if name and mine else where == self.endpoint().get("endpoint"):
        return ""
    if not name and not where:
      return ""
    return " of the robot" + (f" {name!r}" if name else "") + (f" at {where}" if where else "")

  # -- the task folder's references --------------------------------------------------------------

  def reference_match(self, layout: dict[str, Any]) -> dict[str, Any]:
    """This layout's deck (each labware type and where it sits; names, liquids, volumes and aliases do not count)
    against the folder's references for this device: {"matches": the one it matches}, {"closest": the one with the
    fewest differences, "differences": [...]}, {} when there is none, {"unreadable": why}. Never raises."""
    try:
      mine = self.adapter.placement(layout)
      if mine is None:  # a device whose layouts cannot be compared: every new one is the person's
        return {}
      best: dict[str, Any] = {}
      for name, reference in self.ws.reference_layouts(self.spec.model):
        differences = deck_differences(mine, self.adapter.placement(reference) or {})
        if not differences:
          return {"matches": name}
        if not best or len(differences) < len(best["differences"]):
          best = {"closest": name, "differences": differences}
      return best
    except Exception as exc:  # noqa: BLE001 - references that cannot be read leave the layout to the person
      return {"unreadable": f"{type(exc).__name__}: {exc}"}

  def require_tips_known(self) -> None:
    """Real hardware: load_layout starts a new run on the robot, which forgets any tips on the pipettes, so its
    next pick-up would drive them into a rack. Refused while tips may be on (tips_unconfirmed)."""
    if self.adapter is None or self.adapter.simulated:
      return
    doubt = self.tips_note()
    if doubt is not None:
      raise LabError("tips_unconfirmed",
                     f"{doubt} load_layout starts a new run on the robot, whose next pick-up would drive any tip still "
                     "on into a rack, so it waits for the person.",
                     "Tell the person exactly that: they remove any tips by hand (or there are none). Then call "
                     "verify(check='tips_mounted', expect='none') without device and record_verdict for it with its "
                     "ask_person right away. load_layout goes ahead once they pass it. " + HOMING)
    if self.tips_on:
      raise LabError("tips_unconfirmed",
                     f"{_tips_count(self.tips_on)} in this run: load_layout starts a new run on the robot, which "
                     "forgets them, so its next pick-up would drive them into a rack.",
                     "Call drop_tips first (discard or return): this run still knows its tips, so there is nothing "
                     "to ask the person.")

  def pipettes_empty(self) -> None:
    """The person confirmed that no tip is on any pipette: the note goes, and the live run's stop or unknown
    outcome no longer holds load_layout back. A run that knows its tips keeps them (drop_tips drops them)."""
    key = self._doubt_key()
    self._tips_cleared = key
    if key is not None or self.live_run() is None:
      self.tips_on, self._tips_why = {}, None
    self.ws.clear_tips_left()

  def _pending_tips_check(self) -> str | None:
    """The check for every pipette that waits for the person's verdict (the newest), if one does."""
    if self.verification is None:
      return None
    waiting = [cid for cid, r in self.verification.pending.items()
               if r["check"] == "tips_mounted" and "pipettes" in (r["evidence"]["digital_twin"] or {})]
    return waiting[-1] if waiting else None

  def _tips_step(self) -> str:
    """Before load_layout on real hardware, while it is needed: the person's word that the pipettes are empty. With no
    device bound yet, a robot's note waits behind the config step (_tips_later): its check needs a device."""
    doubt = self.tips_note() if self.adapter is not None else None
    if doubt is None:
      return ""
    waiting = self._pending_tips_check()
    if waiting:
      return (f"{doubt} Tips check {waiting} waits for the person's verdict: {pass_now(waiting, homes=True)} "
              "load_layout waits for it. ")
    return (f"{doubt} Tell the person exactly that; they remove any tips by hand (or there are none). "
            "verify(check='tips_mounted', expect='none') without device gives ask_person for it; record_verdict right "
            "away, and load_layout waits for the pass. " + HOMING + " ")

  def tips_warning(self, result: dict[str, Any]) -> dict[str, Any]:
    """A pending deck or tips check carries what the person is told about tips the robot no longer knows of."""
    doubt = self.tips_note()
    if doubt and result.get("verdict") == "pending" and result.get("check") in ("deck_matches_layout", "tips_mounted"):
      return {**result, "next": f"{result.get('next', '')} {doubt}".strip()}
    return result

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
    out.update(status=status, reason=why,
               devices=[{"id": d.id, "model": d.model, "backend": d.backend, **({"options": d.options} if d.options else {})}
                        for d in self.config.devices],
               limits=self.config.limits, confirmed_by=(self.config.confirmation or {}).get("by"))
    if self.config.cameras:
      out["cameras"] = [{"name": c.name, "device": c.device, **({"sees": c.sees()} if c.slot_map else {})}
                        for c in self.config.cameras]
    if self.config.auto_verify:
      out["auto_verify"] = self.config.auto_verify.data()
    if self.config.devices:
      out["confirm_summary"] = confirm_summary(self.config)
    if self.simulates_instead():
      out["note"] = ("This server forces simulation: the config's real device runs on this task's simulator, never "
                     "on the robot it names. A server started without AHC_BACKEND=sim (or --backend sim) follows this "
                     "folder's config and drives the robot it names.")
    return out

  # -- the person's go-ahead for this server session --------------------------------------------

  def session(self) -> dict[str, str]:
    """Whether this server session still needs the person's go-ahead before anything moves. On a real robot it
    does, once per session (one server process: a restart, a resumed or reconnected client is a new one),
    because load_layout homes the robot; in simulation only in a folder that has never run an experiment;
    never where the server forces simulation. The go-ahead is the person's confirm_config in this session. A device
    the agent let go of (disconnect) needs it again, in simulation too."""
    if self.disconnected is not None and (self.config is None or self._go_ahead != self.config.digest()):
      return {"go_ahead": "needed", "why": "the device was disconnected; the person's confirm_config lets it connect again"}
    if self.forced_sim:
      return {"go_ahead": "not needed", "why": "this server forces simulation"}
    config = self.config
    if config is None or not config.devices:
      return {"go_ahead": "needed", "why": "no usable device config yet: the person's confirm_config of the device "
                                           "they choose gives it"}
    if self._go_ahead == config.digest():
      return {"go_ahead": "given", "why": "the person approved confirm_config for this config in this session"}
    robot = self._real_robot()
    if robot is not None:
      return {"go_ahead": "needed", "why": f"the real robot {robot} homes on load_layout"}
    if self.ran_experiment():
      return {"go_ahead": "not needed", "why": "a simulation in a folder that has run an experiment before"}
    return {"go_ahead": "needed", "why": "this folder has never run an experiment"}

  def _real_robot(self) -> str | None:
    """The real device this session drives ('name' at host:port), or None when it simulates: the bound
    adapter once the config is confirmed, else the config's entry."""
    entry = self.config.devices[0]
    bound = self.adapter is not None and self.config.status(self.forced_sim)[0] == "confirmed"
    real = not self.adapter.simulated if bound else entry.backend != "sim"
    if not real:
      return None
    name = entry.options.get("robot_name")
    where = (f" at {entry.options['host']}:{entry.options.get('port') or DEFAULT_PORT}"
             if entry.options.get("host") else "")
    return (repr(name) if name else f"{entry.id} ({entry.model})") + where

  def ran_experiment(self) -> bool:
    if not self._ran:
      self._ran = self.ws.ran_experiment()
    return self._ran

  def require_go_ahead(self) -> None:
    session = self.session()
    if session["go_ahead"] != "needed":
      return
    raise LabError("session_unconfirmed",
                   f"this server session has no go-ahead from the person yet ({session['why']}), so nothing moves "
                   "before they give it; every new session asks once (a restarted server, a resumed or reconnected "
                   "session is a new one).",
                   confirm_now("<config.confirm_summary>", homes=self._real_robot() is not None))

  def _go_ahead_step(self) -> str:
    """The go-ahead, first in next while a confirmed config waits for it (confirming a pending config gives it too)."""
    if self.config is None or self.config.status(self.forced_sim)[0] != "confirmed":
      return ""
    session = self.session()
    if session["go_ahead"] != "needed":
      return ""
    return (f"This session needs the person's go-ahead before anything moves ({session['why']}): "
            + confirm_now(homes=self._real_robot() is not None) + " ")

  def model(self) -> str | None:
    return self._device_and_backend()[0]

  def simulates_instead(self) -> bool:
    """Forced simulation stands in for a real device the config names."""
    return self.forced_sim and self.config is not None and any(d.backend != "sim" for d in self.config.devices)

  def endpoint(self) -> dict[str, Any]:
    """Where the bound device is reached, for adapters that reach one over the network."""
    host, port = getattr(self.adapter, "host", None), getattr(self.adapter, "port", None)
    return {"endpoint": f"{host}:{port}"} if host and port else {}

  def require_device(self) -> DeviceSpec:
    if self.problem is not None:
      raise LabError(self.problem.code, self.problem.message, self.problem.hint)
    choose = ("Ask the person whether to simulate or use a real robot, and which of available_models (lab_overview); "
              "write their choice with configure_devices; then " + confirm_now())
    if self.config is None:
      raise LabError("no_config", "this task folder has no AHC config yet.", choose)
    status, why = self.config.status(self.forced_sim)  # an in-memory simulation config is confirmed
    if status != "confirmed" or self.spec is None:
      raise LabError("config_pending", f"the workspace config is not confirmed ({why}); nothing may move.",
                     "Show the person config.confirm_summary in chat; " + confirm_now("<that summary>")
                     if self.config.devices
                     else choose)
    return self.spec

  def next_step(self) -> str:
    # A robot-side outcome nobody knows (a device error after the command may have reached the robot, or the
    # emergency stop cutting a call short) comes first: the person checks before anything else.
    unknown = getattr(self.adapter, "outcome_unknown", None) or self.interrupted
    outcome = (f"The outcome of {unknown.get('op')} on the robot is unknown: stop and tell the person; they check "
               "tips and wells; then load_layout again with the volumes they report." if unknown else "")
    halted = getattr(self.adapter, "halted", False)
    halting = halted or getattr(self.adapter, "halt_requested", False)
    stop = "The emergency stop stopped the robot's run" if halted else STOP_UNCONFIRMED
    if self.estop is not None:
      return "The emergency stop is engaged: stop, tell the person and wait. Only the person releases it." + (
        " " + outcome if outcome else "")
    go = self._go_ahead_step()  # nothing moves before it: named first
    if (outcome or halting) and self.adapter is not None and not self.adapter.simulated:
      return go + self._after_doubt(unknown, stop if halting else None)
    if outcome:
      return go + outcome + (f" {stop}. The person also removes any tips left on the pipettes first." if halting else "")
    if halting:
      return go + (stop + ". The person removes any tips left on the pipettes, then load_layout again; the deck is "
                   "checked before anything moves.")
    tips = self._tips_step()
    # The person's tips and liquid checks that hold motion (the tips check named above is not named twice).
    held = self.verification.holding(skip=self._pending_tips_check() if tips else None) if self.verification else ""
    return (go + tips + held + self._next_by_state(held=bool(held)) + self._tips_later()).rstrip()

  def _tips_later(self) -> str:
    """No device bound yet (the config waits, or none): a robot's tips note, as what comes once one is."""
    doubt = self.tips_note() if self.adapter is None else None
    return (f" {doubt} On a real robot load_layout waits for the person's pass that the pipettes are empty, asked once "
            "the device is configured." if doubt else "")

  def _after_doubt(self, unknown: dict[str, Any] | None, stop: str | None) -> str:
    """Real hardware after a stop or an unknown outcome: what the person checks, then a new run (load_layout,
    which waits for their pass that the pipettes are empty)."""
    first = (f"The outcome of {unknown.get('op')} on the robot is unknown: stop and tell the person; they check tips "
             "and wells." if unknown else "")
    if stop:
      first = f"{first} {stop}.".strip()
    volumes = " with the volumes they report" if unknown else ""
    waiting = self._pending_tips_check()
    # The person's other checks that hold motion (of one pipette, of liquid) still do.
    held = self.verification.holding(skip=waiting) if self.verification else ""
    if self._doubt_key() == self._tips_cleared:
      return (f"{first} {held}The person confirmed the pipettes are empty: load_layout again{volumes}; the deck is "
              "checked before anything moves.")
    if waiting:
      return (f"{first} {held}Tips check {waiting} waits for the person's verdict: {pass_now(waiting, homes=True)} "
              f"Then load_layout again{volumes}.")
    count = f" ({_tips_count(self.tips_on)})" if self.tips_on else ""
    return (f"{first} {held}The person removes any tips left on the pipettes{count} by hand: verify(check='tips_mounted', "
            "expect='none') without device gives ask_person for it (it replaces a waiting check of one pipette); "
            f"record_verdict right away, and load_layout waits for the pass; then load_layout again{volumes}; the deck "
            "is checked before anything moves. " + HOMING)

  def _next_by_state(self, held: bool = False) -> str:
    """What comes next by the config, the deck check and the plan; `held`: a person's check holds motion, so
    liquid handling is not open whatever the rest says."""
    status = self.config_status()["status"]
    if status == "invalid":
      return ("The config cannot be used (see config.reason): tell the person; they fix .ahc/config.yaml, or you write "
              "it again with configure_devices once they say what it should hold; then confirm_config.")
    plan = "" if self.plan.steps else "Report your plan with report_decision(plan=[...]) first; then "
    if status == "none" and self.forced_sim:
      return plan + "load_layout starts the simulation (configure_devices picks another model before that)."
    if status == "pending" and self.config is not None and self.config.devices:
      real = any(d.backend != "sim" for d in self.config.devices)
      return ("The config waits for the person's approval"
              + (": tell them the robot's identity from configure_devices (name, serial, software, the pipette on each "
                 "mount) in the same message" if real else "")
              + ". " + confirm_now(homes=real))
    if status in ("none", "pending"):
      return ("Ask the person whether to simulate or use a real robot, and which of available_models; write their "
              "choice with configure_devices; then " + confirm_now())
    sim = ("Simulation only: this server forces simulation, so the robot the config names is never driven. "
           if self.simulates_instead() else "")
    if self.gate is not None and self.gate.state != "passed":
      check = self.gate.waiting_for_person()
      if check is not None:
        return sim + (f"Deck check {check} waits for the person's verdict (the layout is on the run dashboard): "
                      f"{pass_now(check, '<ask_person from verify>')} Nothing moves before that.")
      if self.gate.needs_person and self.gate.state == "pending":
        return sim + ("The person confirms this layout before anything moves: verify(check='deck_matches_layout') "
                      "gives ask_person for it; record_verdict right away.")
      return sim + f"The deck check is {self.gate.state}: verify(check='deck_matches_layout') before anything moves."
    if self.gate is None:
      return sim + "find_layout_references; " + plan.lower() + "load_layout, then verify(check='deck_matches_layout')."
    if not self.plan.steps:
      return sim + "Report your plan with report_decision(plan=[...]) before any action."
    return sim + ("" if held else "Liquid handling is open; describe_device(component) for limits.")

  # -- run log --------------------------------------------------------------------------------

  def log_started(self) -> bool:
    """This session already writes a run log."""
    return self.runlog is not None and self.runlog.started

  def record(self, entry: dict[str, Any], defer: bool = False) -> None:
    """Append to this session's run log. defer: write it only into a log that already exists, or keep it
    for this session's first entry that does write, so reading tools never create a run log."""
    if defer and not self.log_started():
      self._deferred.append({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry})
      return
    if self.step_context is not None and ("op" in entry or entry.get("type") == "verification"):
      entry = {**entry, "plan_step": self.step_context}
    if self._call_began is not None and ("op" in entry or entry.get("type") == "verification"):
      began, mono = self._call_began  # `t` is when the call ended; these give its start and length
      entry = {**entry, "t_start": began.isoformat(timespec="milliseconds"),
               "duration_s": round(time.monotonic() - mono, 3)}
    self.open_runlog(entry)
    deferred, self._deferred = self._deferred, []
    for kept in deferred:
      self.runlog.record(kept)
      self.plan.apply(kept)
    self.runlog.record(entry)
    self.plan.apply(entry)
    if "op" in entry:
      self._ops += 1
    if entry.get("type") != "wait":
      self._note_go_ahead()

  def _note_go_ahead(self, defer: bool = False) -> None:
    """Log a `wait` marker when the person's go-ahead (confirm_config) starts to be awaited, once per wait: the
    timing counts the person's time from it to the confirm_config call. A read defers it, so it creates no log."""
    try:
      session = self.session()
    except Exception:  # noqa: BLE001 - timing notes never break a call
      return
    if session["go_ahead"] != "needed":
      self._go_ahead_waiting = False
    elif not self._go_ahead_waiting:
      self._go_ahead_waiting = True
      self.record({"type": "wait", "for": "person", "what": "confirm_config", "reason": session["why"]}, defer=defer)

  def open_runlog(self, entry: dict[str, Any]) -> RunLog:
    """This session's run log, made if it does not exist yet (its file appears with the first entry). `entry` is
    the one about to be recorded."""
    if self.runlog is None:
      self.ws.require_task_folder()  # the run log is a write: never into AHC's own data folder
      self.runlog = RunLog(self.ws.runs_dir, *self._device_and_backend(), extra={"workspace": str(self.ws.root)})
      if self.plan.steps is not None and "plan" not in entry:  # a new log (the device changed): the plan goes on
        carried: dict[str, Any] = {"type": "agent", "decision": "The plan goes on in a new run log", "plan": self.plan.steps,
                                   "visited": sorted(self.plan.visited)}
        if self.plan.current is not None:
          carried["current_step"] = self.plan.current
        self.runlog.record(carried)
    return self.runlog

  def run_images(self, images: list[str]) -> list[str]:
    """Photos named as evidence: files capture wrote in this run's folder, as absolute paths."""
    run_dir = (self.ws.runs_dir / self.runlog.run_id).resolve() if self.runlog is not None else None
    kept = []
    for raw in images:
      path = Path(raw).expanduser()
      path = (path if path.is_absolute() else self.ws.root / path).resolve()
      if run_dir is None or not path.is_relative_to(run_dir):
        raise LabError("image_outside_run", f"{raw!r} is not an image of this run"
                       + (f" (this run's images are under {run_dir})." if run_dir else " (this run has taken no capture yet)."),
                       "Pass only paths that capture returned in this run.")
      if not path.is_file() or path.suffix != ".jpg":
        raise LabError("image_not_found", f"{raw!r} is not an image file capture wrote.",
                       "Pass the paths exactly as capture returned them (images[].path).")
      kept.append(str(path))
    return kept

  # -- cameras and vision -----------------------------------------------------------------------

  def loaded_layout(self) -> Any:
    return getattr(self.adapter, "layout", None)

  def reference_path(self, camera: CameraEntry) -> Path:
    return self.ws.dir / camera.slot_map["reference"]

  def analyze_capture(self, camera: CameraEntry, path: Path) -> dict[str, Any]:
    """One capture against the camera's slot map (blocking: OpenCV). Never raises: a failure is an unclear result."""
    slot_map, reference = camera.slot_map, self.reference_path(camera)
    try:
      if slot_map.get("sha256") and hashlib.sha256(reference.read_bytes()).hexdigest() != slot_map["sha256"]:
        return {"status": "needs_new_map", "why": "the reference image is not the one this map was made on: map the camera again (map_camera)",
                "slots": []}
      layout = self.loaded_layout()
      labware = {k: v["type"] for k, v in vpoints.layout_slots(layout).items()} if layout is not None else None
      return vision.analyze(path, slot_map, reference, labware)
    except Exception as exc:  # noqa: BLE001 - a picture that cannot be analysed is still a picture
      return {"status": "unclear", "why": f"the image could not be analysed ({type(exc).__name__}: {exc})", "slots": []}

  async def take_pictures(self, label: str, plan_step: int | None, cameras: list[str] | None, final: bool,
                          blobs: list[tuple[str, str, bytes]]) -> dict[str, Any]:
    """One still from each named camera (all when None) into the run folder, analysed against its slot map; the
    previews go to `blobs`. Refused where capture is: not on real hardware, no such camera, nobody to show."""
    self.poll_estop()  # a stop is no reason to refuse: a photo after it is evidence
    await self.refresh(write=False)
    self.require_device()
    if self.adapter.simulated:
      raise LabError("camera_not_in_simulation", "this server runs a simulation, and a camera shows the real deck, not the simulator.",
                     "Capture only on real hardware (a robot backend the person confirmed); in simulation use verify and get_state.")
    declared = {c.name: c for c in self.config.cameras}
    if not declared:
      raise LabError("no_cameras", "this task's config declares no cameras.",
                     "Cameras are opt-in: if the person wants them, they name each camera (the name it has on this computer) "
                     "and you write them with configure_devices(cameras=[{'name': 'top', 'device': '<exact device name>'}]); "
                     "they confirm the config.")
    names = list(dict.fromkeys(cameras)) if cameras is not None else list(declared)
    if not names:
      raise LabError("unknown_camera", "cameras is empty.", f"Name cameras from {', '.join(declared)}, or omit it for all.")
    unknown = [n for n in names if n not in declared]
    if unknown:
      raise LabError("unknown_camera", f"no camera {', '.join(repr(n) for n in unknown)} in this config.",
                     f"Cameras: {', '.join(declared)}.")
    if not label.strip():
      raise LabError("bad_label", "label is empty.", "Say in a few words what you want to see.")
    if plan_step is not None:
      self.plan.check(plan_step)
    cams.require_ffmpeg()
    step = plan_step if plan_step is not None else self.plan.current
    run = self.open_runlog({})
    folder = self.ws.runs_dir / run.run_id / cams.step_folder(self.plan.steps, step, final)
    images, failures, rows = [], [], []
    for name in names:
      camera = declared[name]
      started = time.time()
      path = cams.unique_path(folder, name, started)
      why = await asyncio.to_thread(cams.capture_frame, camera.device, path)
      if why is not None:
        failures.append({"camera": name, "device": camera.device, "error": why})
        continue
      image = {"camera": name, "device": camera.device, "path": str(path)}
      try:
        blobs.append((name, camera.device, await asyncio.to_thread(cams.downscaled, path)))
      except OSError as exc:  # the file is on disk, only the preview failed
        image["preview_error"] = str(exc)
      row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)), "run_id": run.run_id,
             "camera": name, "device": camera.device, "file": path.name, "label": label.strip(),
             "plan_step": plan_step, "final": final}
      if camera.slot_map:
        analysis = await asyncio.to_thread(self.analyze_capture, camera, path)
        self.vision_seen[name] = {"t": started, "path": str(path), "analysis": analysis}
        image.update(map=analysis["status"], **{k: analysis[k] for k in ("image_size", "frame_sharpness", "alignment") if k in analysis})
        if analysis["why"]:
          image["why"] = analysis["why"]
        image["slots"] = analysis["slots"]
        row["vision"] = {"status": analysis["status"], "levels": {e["slot"]: e["level"] for e in analysis["slots"]}}
      images.append(image)
      rows.append(row)
    if rows:
      cams.append_captures(folder, rows)
      self.write_auto_state()
    result: dict[str, Any] = {"run_id": run.run_id, "folder": str(folder), "label": label.strip(), "plan_step": plan_step,
                              "final": final, "images": images, "failures": failures}
    if self.estop is not None:
      result["note"] = "The emergency stop is engaged: this capture is evidence only, nothing moves."
    if not images:
      raise LabError("capture_failed", "no camera gave an image: " + "; ".join(f"{f['camera']}: {f['error']}" for f in failures),
                     "Tell the person: check that the cameras are plugged in and not used by another app, and that this "
                     "terminal may use the camera (macOS asks once). Do not retry in a loop.")
    return result

  def vision_cameras(self) -> dict[str, dict[str, Any]]:
    """Each mapped camera with its latest analysis; one from before the last motion shows an older deck: not used."""
    out = {}
    for c in (self.config.cameras if self.config else []):
      if c.slot_map:
        seen = self.vision_seen.get(c.name)
        fresh = seen is not None and seen["t"] > self.last_motion
        out[c.name] = {"map": c.slot_map, "analysis": seen["analysis"] if fresh else None, "captured": seen is not None,
                       "image": (seen or {}).get("path")}
    return out

  def context_sources(self) -> list[Any]:
    """The physical-world context sources now (ahc.physical_world). A balance, a plate reader or room sensors join here.
    The digital twin is not one: it is what observations are compared with."""
    sources: list[Any] = [VisionContext(self.vision_cameras())]
    if self.adapter is not None and self.spec is not None:
      sources.append(InstrumentContext(self.spec.prefix, self.adapter.sensor_report()))
    return sources

  def physical_world(self, points: list[dict[str, str]]) -> dict[str, Any]:
    """The points against every context source: {vision_context, instrument_context, points,
    physical_world_confidence, suggestions}."""
    layout, sources = self.loaded_layout(), self.context_sources()
    result = pw.evaluate(points, lambda what: vpoints.resolve(what, layout), sources)
    return {**{f"{s.kind}_context": s.describe() for s in sources}, **result}

  def vision_points_now(self) -> list[dict[str, str]]:
    """The run's verification points: needs a plan and a loaded layout."""
    layout = self.loaded_layout()
    if self.plan.steps is None or layout is None:
      return []
    pipettes = [f"{self.spec.prefix}.{n}" for n, c in self.spec.components.items() if c.capabilities]
    return vpoints.derive(layout, pipettes, self.vision_points)

  def vision_digest(self) -> str:
    return vpoints.maps_digest(self.config)

  def vision_record(self) -> dict[str, dict[str, Any]]:
    return VisionRecord(self.ws).read(self.vision_digest())

  def physical_world_overview(self) -> dict[str, Any]:
    """lab_overview's part: each source in a line, what the last check concluded per check type, the record."""
    sources: dict[str, str] = {f"vision:{c.name}": c.sees() or "no slot map yet (map_camera)" for c in self.config.cameras}
    if self.adapter is not None and self.spec is not None:
      sources[f"instrument:{self.spec.prefix}"] = self.adapter.sensor_report()["note"]
    result = self.vision_result
    out: dict[str, Any] = {"sources": sources}
    if result:
      for name, seen in result["result"]["physical_world_confidence"]["by_source"].items():
        if name in sources:
          sources[name] += f"; covers {seen['covers']} of {seen['observes']} points it observes"
    if any(c.slot_map for c in self.config.cameras):
      out["summary"] = (pw.summary_line(result["result"], result["at"][11:19]) if result else
                        "not checked yet: physical_world_context captures and checks the verification points")
      if result:
        out["confidence"] = {k: v["status"] for k, v in result["result"]["physical_world_confidence"]["by_check"].items()}
      streaks = {k: v["agree_streak"] for k, v in self.vision_record().items()}
      if streaks:
        out["record"] = {"agree_streak": streaks, "note": "agent image judgments that matched the person's verdicts, in a row"}
    return out

  # -- auto-verify ------------------------------------------------------------------------------

  def auto_streak(self, check: str) -> int:
    return int((self.vision_record().get(check) or {}).get("agree_streak") or 0)

  def auto_rows(self) -> list[dict[str, Any]]:
    """Every verification point of the check types the config lists, against the context sources now."""
    listed = self.config.auto_verify.checks
    points = [p for p in self.vision_points_now() if p["check"] in listed]
    return self.physical_world(points)["points"] if points else []

  def auto_confidence(self, rows: list[dict[str, Any]]) -> str | None:
    """Run level: why auto is off for every check (a point of a listed check type is uncovered or unclear), else None.
    Points of check types the config does not list are not in `rows`, so they never block it."""
    why = pw.reasons(rows)
    return None if why is None else "low vision confidence: " + why

  def auto_coverage(self, check: str, rows: list[dict[str, Any]], targets: list[str] | None = None) -> str | None:
    """Whether this check has anything to judge from: points of its own, and a liquid check's targets inside them.
    (Whether those points are confident is the run level: auto_confidence.) None when it has."""
    layout = self.loaded_layout()
    if not any(c.slot_map for c in self.config.cameras):
      return "no camera has a slot map (map_camera)"
    points = [p for p in self.vision_points_now() if p["check"] == check]
    if not points:
      return (f"there are no {check} verification points yet: " + (
        "declare the targets with report_decision(vision_points=[...])" if check == "liquid_present" else
        "they come from the plan and the loaded layout"))
    if check == "liquid_present" and targets:  # the check's own targets must lie inside the points
      declared = []
      for p in points:
        try:
          declared.append(vpoints.resolve(p["what"], layout))
        except ValueError:
          pass
      for target in targets:
        try:
          wanted = vpoints.resolve(target, layout)
        except ValueError:
          return f"target {target} is not in the loaded layout"
        if not any(d["labware"] == wanted["labware"] and (d["wells"] is None or (
            wanted["wells"] is not None and set(wanted["wells"]) <= set(d["wells"]))) for d in declared):
          return (f"target {target} is in no liquid_present verification point: declare it with "
                  "report_decision(vision_points=[...])")
    return None

  def auto_assess(self, check: str, targets: list[str] | None = None, recovery: bool = False) -> dict[str, Any]:
    """Whether this check is auto now: {why (the first failed condition, None: auto), points (this check's, with their
    sources)}. Real hardware. `recovery`: the check of every pipette (after a stop, an unknown outcome or a restart)."""
    rows = self.auto_rows()
    why = auto.off_reason(check, self.config.auto_verify, pause=auto.paused(self.ws), stopped=self.estop is not None,
                          recovery=recovery, confidence=self.auto_confidence(rows),
                          coverage=self.auto_coverage(check, rows, targets), streak=self.auto_streak(check))
    return {"why": why, "points": [r for r in rows if r["check"] == check]}

  def write_auto_state(self) -> None:
    """The run-level confidence and the coverage per authorised check type, for the dashboard's badges. Never raises."""
    try:
      if (self.config is None or self.config.auto_verify is None or self.adapter is None or self.adapter.simulated
          or self.spec is None):
        return
      rows = self.auto_rows()
      auto.write_coverage(self.ws, {c: self.auto_coverage(c, rows) for c in self.config.auto_verify.checks},
                          self.auto_confidence(rows))
    except Exception:  # noqa: BLE001 - a badge must never break a call
      pass

  def auto_view(self, result: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The pending auto check as the agent sees it: the points with the crops to judge, and what to call."""
    layout, cameras = self.loaded_layout(), self.vision_cameras()
    points, images = [], []
    for row in rows:
      crops = []
      try:
        key = vpoints.resolve(row["what"], layout)["key"]
      except ValueError:
        key = None
      for name in [e["source"].partition(":")[2] for e in row["sources"] if e["covers"] and e["source"].startswith("vision:")]:
        entry = next((e for e in (cameras[name]["analysis"] or {}).get("slots", []) if e["slot"] == key), None)
        if entry and entry.get("crop"):
          crops.append(entry["crop"])
      images += [c for c in crops if c not in images]
      points.append({k: row[k] for k in ("what", "level", "status", "sources") if k in row} | {"crops": crops})
    shown = {k: v for k, v in result.items() if k not in ("ask_person", "next")}
    return {**shown, "auto": True, "points": points, "images": images,
            "ask_agent": ("The config lets the cameras answer this check: judge it yourself from the crops in `images` (per point in "
                          f"`points`), which AHC measured as sharp and aligned enough. What it asks: {result['ask_person']} "
                          "Pass only if every point shows it; if anything is hidden, blurred, off or you cannot tell, say fail or "
                          "unclear: the person then decides, nothing is recorded as a verdict and nothing moves."),
            "next": agent_now(result["check_id"])}

  def hand_back(self, check_id: str, why: str, agent: dict[str, Any] | None = None) -> dict[str, Any]:
    """An auto check goes to the person (the conditions no longer hold, or the agent said fail or unclear): it is a plain
    pending person check, motion still held. `agent`: its judgment {verdict, images, per_point, note}, shown to the
    person and compared with their verdict like a shadow proposal. Nothing is recorded as a verdict."""
    self.auto_checks.pop(check_id, None)
    if self.gate is not None and self.gate.check_id == check_id:
      self.gate.auto = False
    result = self.verification.hand_to_person(check_id, f"auto-verify is off for this check ({why})")
    out = {**result, "auto": False, "auto_off": why}
    if agent is not None:
      self.proposals[check_id] = {**agent, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
      out["agent_judgment"] = agent
      self.record({"type": "vision", "event": "handback", "check_id": check_id, "check": result["check"], "why": why,
                   "verdict": agent["verdict"], "images": agent["images"], "per_point": agent["per_point"], "note": agent["note"]})
    else:
      self.record({"type": "vision", "event": "handback", "check_id": check_id, "check": result["check"], "why": why})
    out["next"] = f"This check is the person's now ({why}). " + result["next"]
    return out

  def require_current_deck_check(self, check_id: str, pending: dict[str, Any] | None) -> None:
    """A deck check made for an earlier load or deck check can never open anything now: refused, and dropped."""
    gate = self.gate
    if pending is not None and pending["check"] == "deck_matches_layout" and (gate is None or gate.check_id != check_id):
      self.verification.pending.pop(check_id)
      now = gate.waiting_for_person() if gate is not None else None
      raise LabError("check_superseded",
                     f"deck check {check_id} was made for an earlier load or deck check, so a verdict on it would change nothing.",
                     (f"The gate waits on deck check {now}: "
                      + (agent_now(now) if gate.auto else pass_now(now, "<its ask_person>")) if now else
                      "Run verify(check='deck_matches_layout') again, then record_verdict for that new check with its "
                      "ask_person."))

  def auto_line(self) -> str:
    """lab_overview's one line on auto-verify: each authorised check type AUTO or why it is off."""
    cfg = self.config.auto_verify
    if self.adapter is None or self.adapter.simulated:
      return f"{cfg.summary()}: not used in simulation (the simulator judges), only on real hardware"
    parts = []
    for check in cfg.checks:
      why = self.auto_assess(check)["why"] if self.spec is not None else "no device yet"
      parts.append(f"{check} " + ("AUTO" if why is None else f"with the person ({why})"))
    confidence = self.auto_confidence(self.auto_rows()) if self.spec is not None else None
    run = "Run level: " + ("auto is on" if confidence is None else f"auto is off for every check ({confidence})")
    return f"{cfg.summary()}. {run}. Now: " + "; ".join(parts)

  def record_refusal(self, op: str, args: dict[str, Any], exc: LabError) -> None:
    """A refused call in the run log too; a folder that can hold no run log (AHC's own) just refuses."""
    try:
      self.record({"op": op, "basic": args, "status": "refused", "error": exc.code, "message": str(exc)})
    except LabError:
      pass

  def _device_and_backend(self) -> tuple[str | None, str | None]:
    """What this session drives, for a run log's header: the bound device, else the config's."""
    if self.spec is not None:
      return self.spec.model, self.adapter.backend
    entry = self.config.devices[0] if self.config and self.config.devices else None
    return (entry.model if entry else None), ("sim" if self.forced_sim else (entry.backend if entry else None))

  def summary(self) -> dict[str, Any]:
    if self.runlog is None:
      self.runlog = RunLog(self.ws.runs_dir, *self._device_and_backend(), extra={"workspace": str(self.ws.root)})
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

  async def shielded(self, op: str, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """A call that changes something (motion, the device config), under the lock. A client that stops
    waiting (Esc, a timeout) never cuts it short: the call runs to its end with the lock held, its true
    outcome is recorded, and only then does the client's cancellation go on (it gets no reply)."""
    async def body():
      try:
        with anyio.CancelScope(shield=True):
          return await fn()
      finally:
        if anyio.current_effective_deadline() == -math.inf:  # the request was cancelled meanwhile
          self._note({"type": "audit", "event": "request_cancelled", "call": op,
                      "detail": f"the client stopped waiting for {op} (or the server is shutting down); the call ran "
                                "to its end and its outcome is recorded above, but the agent never saw it"})
          await anyio.lowlevel.checkpoint()  # the cancellation goes on from here
    return await self.run(body)

  async def act(self, fn: Callable[[], Awaitable[dict[str, Any]]], op: str, args: dict[str, Any],
                plan_step: int, gated: bool = True) -> dict[str, Any]:
    """A tool that may move something: not stopped, config confirmed, a plan reported, deck check passed.

    The work runs as a child task that only the emergency stop cancels, so the client gets a proper
    refusal instead of a broken call; a client that stops waiting never cancels it (see shielded).
    What the call records carries its plan step; a call refused here never starts its step.
    """
    return await self.shielded(op, lambda: self._act(fn, op, args, plan_step, gated))

  async def _act(self, fn, op: str, args: dict[str, Any], plan_step: int, gated: bool) -> dict[str, Any]:
    try:
      self.require_not_stopped()
      await self.refresh(write=True)
      self.require_device()
      self.require_connected(op)
      self.require_go_ahead()
      self.plan.check(plan_step)
      if self.verification is not None:  # a person's tips or liquid check that waits for them, or that they failed
        self.verification.require_unheld(op.split(":")[0])
      if gated:
        self.require_gate()
    except LabError as exc:  # safety refusals belong in the run log too
      self.record({"op": op, "device": args.get("device"), "basic": args, "status": "refused",
                   "error": exc.code, "message": str(exc)})
      raise
    last = self.last_step_starts(plan_step)
    before = self.lh._twin_volumes() if self.lh else {}
    known = getattr(self.adapter, "outcome_unknown", None)
    ops = self._ops
    self.step_context = plan_step
    self.last_motion = time.time()
    noted = self._picking_up(args.get("device")) if op == "pick_up_tips" else None  # on disk before the robot has them
    task = asyncio.ensure_future(fn())
    self.current_action, self.current_call = task, (op, args)
    try:
      try:
        result = await task
      except asyncio.CancelledError:
        if not (task.cancelled() and self.estop is not None):
          task.cancel()
          raise
        raise self._interrupted(op, args, before) from None
      except LabError as exc:
        if self._ops == ops:  # refused before the layer below logged anything (e.g. an unknown component)
          self.record({"op": op, "device": args.get("device"), "basic": args, "status": "refused",
                       "error": exc.code, "message": str(exc)})
        failed = getattr(self.adapter, "outcome_unknown", None)
        if failed is not None and failed is not known:  # this call's outcome on the robot is unknown
          self._tips_event(f"an unknown outcome of {op} at {str(failed.get('at') or '')[11:16]}", (op, args))
        elif noted is not None:  # refused before anything was sent to the robot
          self._put_back(noted)
        raise
      if op == "drop_tips":  # a failed tips check of this pipette holds it back no longer
        self.verification.tips_dropped(args["device"])
      if op in ("pick_up_tips", "drop_tips"):
        self._tips_changed()
      return self.plan_note(result, last)
    finally:
      self.current_action = self.current_call = None
      self.step_context = None
      self.last_motion = time.time()
      if op in DECK_OPS:
        self._save_deck()
      self.write_auto_state()  # the cameras' captures are older than this motion now

  def _interrupted(self, op: str, args: dict[str, Any], before: dict[str, float]) -> LabError:
    """The emergency stop cut a call short; recorded, and the refusal to return. In simulation the twin
    shows what already moved. On real hardware the outcome is unknown: the robot may finish the
    command in flight while the twin rolls it back, and the tips may be left down in a well."""
    by = self.estop.get("by")
    record: dict[str, Any] = {"op": op, "device": args.get("device"), "basic": args, "status": "refused"}
    if self.adapter is not None and not self.adapter.simulated:
      record["partial"] = "unknown"
      self.interrupted = {"op": op, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "call": args}
      error = LabError("emergency_stop",
                       f"the emergency stop interrupted {op} ({by}); its outcome on the robot is unknown: the robot may "
                       "have finished the command in flight, the digital twin may not show it, and the tips may be "
                       "left down in a well. Nothing runs until the person releases it.",
                       "Stop now and tell the person. Do not retry, work around it or release it yourself. The person "
                       "checks the tips and wells before anything else.")
    else:
      after = self.lh._twin_volumes() if self.lh else {}
      moved = {k: round(after[k] - v, 3) for k, v in before.items() if k in after and abs(after[k] - v) > 1e-6}
      if moved:
        record["partial"] = moved
      error = LabError("emergency_stop",
                       f"the emergency stop interrupted {op} ({by})"
                       + (f"; partly done: {', '.join(f'{k} {d:+g} uL' for k, d in list(moved.items())[:8])}" if moved else "")
                       + ". Nothing runs until the person releases it.",
                       "Stop now and tell the person. Do not retry, work around it or release it yourself.")
    self.record({**record, "error": error.code, "message": str(error)})
    return error

  def last_step_starts(self, plan_step: int) -> bool:
    return bool(self.plan.steps) and plan_step == len(self.plan.steps) and plan_step not in self.plan.visited

  def plan_note(self, result: dict[str, Any], last: bool) -> dict[str, Any]:
    """The first call of the plan's last step reminds the agent how the plan ends."""
    if not last:
      return result
    n = len(self.plan.steps)
    return {**result, "plan": f"Step {n} of {n}, the last of your plan: when it is done, report the result with "
                              "report_decision(plan_done=true)."}

  def gate_changed(self, reason: str, defer: bool = False) -> None:
    self.record({"type": "gate", **self.gate.public(), "reason": reason}, defer=defer)

  def require_gate(self) -> None:
    """Motion only on the deck whose check passed: the gate must belong to the layout the device holds
    now. A loaded layout without a gate, or a gate of another layout, counts as not checked."""
    layout = getattr(self.adapter, "layout", None)
    if self.gate is None:
      if layout is not None:
        raise LabError("gate_pending", "a layout is loaded but its deck check never started, so nothing may move.",
                       "Load the layout again with load_layout; the deck is checked before anything moves.")
      return
    if self.gate.layout_ref is not layout:
      raise LabError("gate_pending", "the deck check belongs to another layout than the one the device holds now, so "
                     "nothing may move.", "Load the layout again with load_layout; the deck is checked before anything moves.")
    self.gate.require_open()

  async def read(self, fn: Callable[[], Any]) -> Any:
    """A read-only tool: never creates anything in the workspace."""
    async def body():
      await self.refresh(write=False)
      result = fn()
      return await result if inspect.isawaitable(result) else result
    return await self.run(body)

  async def device_identity(self) -> dict[str, Any]:
    """What the bound device says it is; blocking HTTP on a robot, so off the event loop."""
    try:
      return await asyncio.to_thread(self.adapter.identity)
    except Exception as exc:  # noqa: BLE001 - who the device is must never break the call
      out = {"error": f"{type(exc).__name__}: {exc}"}
      if isinstance(exc, OSError) and not isinstance(exc, urllib.error.HTTPError):  # nothing answers at the address
        try:  # ... but perhaps the robot answers at another one
          moved = await self.renamed(LabError("backend_unreachable", out["error"]))
        except Exception:  # noqa: BLE001
          return out
        out = {"error": moved.message, **({"hint": moved.hint} if moved.hint else {})}
      return out

  async def record_identity(self) -> dict[str, Any]:
    """A real device's identity, in the run log once per binding."""
    identity = await self.device_identity()
    if identity and self._identified != self._bound:
      self._identified = self._bound
      self.record({"type": "device", "model": self.spec.model, "backend": self.adapter.backend, **self.endpoint(),
                   **identity})
    return identity

  # -- views ----------------------------------------------------------------------------------

  def stop_ways(self) -> str:
    """How a person stops this server: its open dashboard, and the command that works without PATH."""
    root = str(self.ws.root)
    command = f"ahc-estop --workspace {shlex.quote(root)}"
    if (REPO / "pyproject.toml").is_file():  # a source checkout: the commands live in its .venv, not on PATH
      command = f"uv run --project {shlex.quote(str(REPO))} {command}"
    page = (f"the stop button on the dashboard at {self.dashboard['url']}" if self.dashboard
            else "the stop button on the dashboard (open_dashboard opens it)")
    return f"{page}, or in a terminal: {command}"

  async def overview(self) -> dict[str, Any]:
    self._note_go_ahead(defer=True)
    root = str(self.ws.root)
    stop = {"engaged": True, **self.estop} if self.estop else {"engaged": False}
    stop_way = self.stop_ways()
    stop["how"] = (f"The emergency stop for this server: {stop_way}. Tell the person; a stop pressed for another folder "
                   "does not reach it.")
    if self.adapter is not None and not self.adapter.simulated:  # what the stop can and cannot do on a real robot
      stop["how"] += (" On a real OT-2 it ends AHC's run, but the robot first finishes the command it is executing (a "
                      "move, an aspirate, a pick-up); the power switch is the immediate stop.")
    out: dict[str, Any] = {"workspace": root, "forced_simulation": self.forced_sim, "config": self.config_status(),
                           "session": self.session(), "emergency_stop": stop,
                           "dashboard": ({"url": self.dashboard["url"]} if self.dashboard else
                                         {"url": None, "hint": "Call open_dashboard so the person can watch and stop the run."})}
    if self.config is not None and self.config.cameras:
      out["physical_world_context"] = self.physical_world_overview()
    if self.config is not None and self.config.auto_verify:
      out["auto_verify"] = self.auto_line()
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project and Path(project).expanduser().resolve() != self.ws.root:
      out["warning"] = (f"This Claude Code session's project folder (CLAUDE_PROJECT_DIR) is {project}, but this server's "
                        f"workspace is {root}: a stop pressed for {project} does not reach this server. Tell the person "
                        f"the stop for this server is {stop_way}.")
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
    if spec.mounted:
      out["mounts"] = _mounts_info(spec)
    out.update(device=spec.model, id=spec.prefix, title=spec.title, backend=self.adapter.backend,
               simulated=self.adapter.simulated, backend_detail=spec.backends[self.adapter.backend],
               **self.endpoint(), components=comps, layout_format=_layout_format(spec), checks=CHECKS,
               live_view={"enabled": self.view is not None, "url": self.view.url if self.view else None},
               next=self.next_step())
    if not self.adapter.simulated:  # on real hardware the person sees which instrument this is
      out["device_identity"] = await self.device_identity()
    last = self.last_deck()
    if last is not None:
      out["last_deck"] = {"run_id": last["run_id"], "at": last["at"], "unknown": len(last["unknown"]),
                          "next": "find_layout_references has it as a ready layout (last_deck), to continue from it."}
    sim = SIMULATORS.get(spec.model) if self.adapter.simulated else None
    if sim is not None:
      out["simulator"] = {**await asyncio.to_thread(sim.status, self.ws), "start": sim.start_command(self.ws),
                          "setup": f"{sim.ahc_sim_command()} ot2 --setup"}
    return out

  def last_deck(self) -> dict[str, Any] | None:
    """The deck the last run here ended with, as a layout to load (deck_state.last_deck), or None: none saved, or it
    was a run on another device model."""
    deck = self.ws.read_deck()
    model = self.model()
    if deck is None or (model is not None and (deck.get("device") or {}).get("model") != model):
      return None
    try:
      return deck_state.last_deck(deck)
    except (KeyError, TypeError, ValueError, AttributeError):  # a file that was edited by hand
      return None

  def unknown_state(self) -> dict[str, Any] | None:
    """What the digital twin cannot vouch for on real hardware, or None. After a call whose outcome is unknown
    (a device error once it was sent, or the emergency stop cutting it short) or a stop of the robot's run: the
    tips on the pipettes (the robot forgets them when its run stops), the wells that call touched and, after a
    tip call, the tips left in the racks. `why` is the line that says so and what the person checks; while the
    robot has not confirmed the stop, it says first that the robot may still be moving. In simulation the twin
    is the simulator's own state, so never."""
    adapter = self.adapter
    if adapter is None or adapter.simulated:
      return None
    failed, cut = getattr(adapter, "outcome_unknown", None), self.interrupted
    halted = getattr(adapter, "halted", False)
    unconfirmed = getattr(adapter, "halt_requested", False) and not halted
    if not (failed or cut or halted or unconfirmed):
      return None
    record = failed or cut or {}
    op, call, layout = record.get("op"), record.get("call") or {}, adapter.layout
    wells: list[Container] = []
    if layout is not None and call.get("targets"):
      try:
        wells = layout.resolve_targets(list(call["targets"]))
      except LabError:  # a target the call was refused for: it touched nothing
        pass
    racks = op == "pick_up_tips" or (op == "drop_tips" and call.get("mode") == "return")
    spots: list[Any] = []
    if layout is not None and op == "pick_up_tips" and isinstance(call.get("tips"), list):
      try:
        spots = layout.resolve_tips(list(call["tips"]))  # named tips: these spots are unknown, not the whole rack
      except LabError:
        pass
    if failed:
      kind = str(failed.get("error") or "a device error").split(":")[0]
      cause = f"The outcome of {op} on the robot is unknown ({kind} after it was sent)"
    elif cut:
      cause = f"The emergency stop interrupted {op} on the robot, which may have finished it"
    elif halted:
      cause = "The emergency stop stopped the robot's run, and the robot forgets which tips are on when it stops"
    else:
      cause = "Once its run stops, the robot forgets which tips are on"
    what = (["the tips on the pipettes"] + ([f"the liquid in {layout.where(wells)}"] if wells else [])
            + (["the tips left in the racks"] if racks else []))
    listed = what[0] if len(what) == 1 else ", ".join(what[:-1]) + " and " + what[-1]
    why = (f"{cause}, so {listed} are not known here: the person checks them on the robot (and removes any tips "
           "from the pipettes), then load_layout again" + (" with the volumes they report." if wells else "."))
    if unconfirmed:  # the robot may still be moving: that comes first
      why = f"{STOP_UNCONFIRMED}. {why}"
    return {"why": why, "wells": wells, "racks": racks, "spots": spots}

  def state(self, device: str | None) -> dict[str, Any]:
    if self.spec is None:
      return {"configured": False, "config": self.config_status()}
    names = [device] if device else [n for n, c in self.spec.components.items() if c.capabilities]
    comps = [self.spec.component(n) for n in names]
    doubt = self.unknown_state()  # real hardware after an unknown outcome or a stop: not "0 tips", not exact volumes
    out: dict[str, Any] = {"simulated": self.adapter.simulated, "gate": self.gate.public() if self.gate else None,
                           "tips_mounted": {f"{self.spec.prefix}.{c.name}": "unknown" if doubt else
                                            len(self.adapter.mounted_tips(c)) for c in comps}}
    if doubt:
      out["unknown"] = doubt["why"]
    layout = self.adapter.layout
    if layout is None:
      out["layout"] = None
      return out
    unknown = {id(w) for w in doubt["wells"]} if doubt else set()
    # returned: tips back in their spots after use, never next fresh, only when named
    liquids, tips_left, returned = deck_state.contents(layout, unknown, bool(doubt and doubt["racks"]))
    out.update(liquids_ul=liquids, tips_left=tips_left)
    if returned:
      out["tips_returned"] = returned
    return out


def _layout_format(spec: DeviceSpec) -> dict[str, Any]:
  kind = spec.layout["kind"]
  return {"kind": kind, "rules": {k: v for k, v in spec.layout.items() if k != "kind"},
          "example": LAYOUT_EXAMPLES[kind]}


def _model_description(spec: DeviceSpec) -> dict[str, Any]:
  out = {"device": spec.model, "title": spec.title, "backends": spec.backends, "guide": spec.guide,
         "components": {f"{spec.prefix}.{n}": c.public() for n, c in spec.components.items()}}
  if spec.raw.get("mounts"):  # what the mounts carry, and every instrument the file describes
    out["mounts"] = {**_mounts_info(spec), "instruments": {n: i.get("title", n) for n, i in spec.instruments.items()}}
  return out


def _mounts_info(spec: DeviceSpec) -> dict[str, Any]:
  """What each mount carries (None: empty), and the ones carrying a pipette AHC does not drive."""
  out: dict[str, Any] = {"mounted": spec.mounted}
  if spec.not_driven():
    out["not_driven"] = spec.not_driven()
  return out


def judged_points(per_point: list[dict[str, str]] | None) -> list[dict[str, str]]:
  """The agent's per-point judgments, checked for shape."""
  points = []
  for i, item in enumerate(per_point or []):
    if not isinstance(item, dict) or item.get("verdict") not in ("pass", "fail", "unclear") or not item.get("what") or len(per_point) > 40:
      raise LabError("bad_per_point", f"per_point[{i}] must be {{'what': ..., 'verdict': 'pass'|'fail'|'unclear'}} (at most 40).",
                     "Give one entry per verification point you judged.")
    points.append({"what": str(item["what"])[:80], "verdict": item["verdict"], **({"note": str(item["note"])[:200]} if item.get("note") else {})})
  return points


def create_server(workspace=None, backend: str | None = None, device: str | None = None,
                  options: dict[str, Any] | None = None, view: bool | None = None) -> MCPServer:
  # 'sim' forces simulation; 'config' (like nothing) follows the task folder's confirmed config. Forcing
  # simulation wins from either source, so AHC_BACKEND=sim still holds where a client passes 'config'.
  modes = {backend, os.environ.get("AHC_BACKEND") or None} - {None}
  for mode in modes:
    if mode not in ("sim", "config"):
      raise ValueError(f"backend {mode!r} cannot be forced: only 'sim' can ('config' follows the task folder's "
                       "config). Real backends come only from a workspace config the person confirmed.")
  if view is None:
    view = os.environ.get("AHC_VIEW") == "1"
  lab = Lab(resolve_workspace(workspace), forced_sim="sim" in modes,
            default_model=device or os.environ.get("AHC_DEVICE") or None, options=options or {}, view=view)

  @asynccontextmanager
  async def lifespan(_server: MCPServer):
    watcher = asyncio.create_task(lab.watch_estop())
    if lab.handle_signals:
      lab.install_signal_handlers()
    try:
      yield lab
    finally:
      watcher.cancel()
      await lab.shutdown()

  server = MCPServer("ahc", instructions=instructions(lab.forced_sim), version=__version__, lifespan=lifespan)
  server.lab = lab  # tests and embedding code reach the live state here

  @server.tool(annotations=READ_ONLY,
               description="Start here: this task's workspace and device config, the device and its components, the layout format, and the next step.")
  async def lab_overview() -> dict[str, Any]:
    return await lab.read(lab.overview)

  @server.tool(annotations=READ_ONLY,
               description="Find OT-2 robots on this computer's networks (an mDNS browse, then each robot's /health) and nothing else: "
                           "name, address, serial, software and the pipettes on its mounts; an AHC simulator is marked simulator. "
                           "The person picks which robot this task uses; configure_devices then reads its identity and mounts for them to confirm. "
                           "Moves nothing; takes a few seconds.")
  async def find_robots(timeout_s: Annotated[float, Field(description="seconds to listen for robots (1 to 15)")] = 4.0) -> dict[str, Any]:
    # Not under the lab lock (it takes seconds and touches no lab state); not stopped by the emergency stop (no motion).
    try:
      started = time.monotonic()
      robots = await asyncio.to_thread(discovery.find_robots, timeout_s)
    except LabError as exc:
      lab.record({"type": "audit", "event": "robots_searched", "status": "refused", "error": exc.code}, defer=True)
      raise
    lab.record({"type": "audit", "event": "robots_searched", "status": "ok", "timeout_s": timeout_s,
                "found": [f"{r['name']} {r['host']}" for r in robots]}, defer=True)
    out: dict[str, Any] = {"robots": robots, "searched_seconds": round(time.monotonic() - started, 1)}
    out["next"] = ("Show the person each robot (name, address, pipettes) and ask which one this task uses; then "
                   "configure_devices with options.host set to its address (a simulator is for tests, not a real robot)."
                   if robots else
                   "No robot answered. It may be off, or on another network: ask the person for its address.")
    return out

  @server.tool(annotations=CONFIGURE,
               description="Open the run dashboard for this task folder, served by this server (it ends with the server), and give the person its URL: "
                           "they watch the run there and its red button is the emergency stop. Call it at the start of a task. Asked again, it returns the same one. "
                           "view_only=true opens a second page for an audience, with no stop or release button (the person keeps the control page). "
                           "Moves nothing; it also works while the emergency stop is engaged.")
  async def open_dashboard(open_browser: Annotated[bool, Field(description="Open the page in the person's browser too")] = True,
                           view_only: Annotated[bool, Field(description="A page without stop or release controls, for an audience")] = False) -> dict[str, Any]:
    # Not under the lab lock and not stopped by the emergency stop: the stop button is on this page.
    out = await lab.open_dashboard(open_browser, view_only)
    lab.record({"type": "audit", "event": "dashboard_opened", "status": "ok", "url": out["url"], "started": out["started"],
                **({"view_only": True} if view_only else {})}, defer=True)
    return out

  @server.tool(annotations=CONFIGURE,
               description="Write this task's device config (.ahc/config.yaml) after the person chose the device: devices (id, model, backend), optional stricter task limits, optional cameras (for capture) and optional auto_verify (the person lets the cameras answer deck_matches_layout, liquid_present or tips_mounted checks; see the lab-hardware skill); all part of what the person confirms. "
                           "It stays pending until the person confirms it with confirm_config; a simulation-only config is confirmed automatically when the server forces simulation.")
  async def configure_devices(devices: DeviceList, limits: TaskLimits = None, cameras: CameraList = None, auto_verify: AutoVerifyArg = None) -> dict[str, Any]:
    async def body():
      lab.require_not_stopped()
      entries = parse_devices(devices)
      if not entries:
        raise LabError("config_invalid", "devices is empty.", "Name the device the person chose.")
      for e in entries:  # entries that cannot run
        check_entry(e)
      identities = {}
      for i, e in enumerate(entries):  # a real device: who answers there, before the person is asked
        entries[i], identity = await probe_entry(e)
        if identity:
          identities[e.id] = identity
      for e in entries:  # limits that would loosen the description file's, or name a component this device lacks
        apply_limits(replace(device_spec(e.model, e.options), id=e.id), limits or {})
      if any(isinstance(c, dict) and "slot_map" in c for c in cameras or []):
        raise LabError("config_invalid", "configure_devices does not write slot maps.",
                       "Leave slot_map out: a camera whose name and device stay keeps its map; map_camera writes or changes one.")
      chosen = parse_cameras(cameras) if cameras is not None else None
      authorised = parse_auto_verify(auto_verify)  # refuses a check a camera may not answer: config_invalid
      if chosen:  # each camera must exist on this computer now
        await asyncio.to_thread(cams.require_devices, [c.device for c in chosen])
      text = lab.ws.read_config_text()
      previous, unreadable = None, False
      if text:
        try:
          previous = parse_config(text)
        except LabError:
          unreadable = True  # its limits are unknown: whatever replaces it waits for the person
      dropped: list[str] = []
      if chosen and previous:  # a camera that keeps its name and device keeps its slot map
        maps = {(c.name, c.device): c.slot_map for c in previous.cameras if c.slot_map}
        chosen = [replace(c, slot_map=maps.get((c.name, c.device))) for c in chosen]
        dropped = [c.name for c in previous.cameras if c.slot_map and not any((c.name, c.device) == (n.name, n.device) for n in chosen)]
      elif chosen is not None and previous:
        dropped = [c.name for c in previous.cameras if c.slot_map]
      kept = chosen if chosen is not None else (previous.cameras if previous else [])  # omitted: the cameras stay
      config = Config(devices=entries, limits=limits or {}, sim=previous.sim if previous else {}, cameras=kept,
                      auto_verify=authorised if auto_verify is not None else (previous.auto_verify if previous else None))
      if previous is not None and previous.confirmation and previous.confirmation.get("digest") == config.digest():
        config.confirmation = previous.confirmation  # the same devices and limits: still confirmed, nothing rebinds
      # Simulation-only and forced: confirmed without the person, unless it drops or loosens task
      # limits the person set, which only the person may do.
      keeps = not unreadable and keeps_limits(previous.limits if previous else {}, config.limits)
      if config.confirmation is None and lab.forced_sim and all(e.backend == "sim" for e in entries) and keeps:
        config.confirm("simulation")
      lab.ws.write_config(config.data(), HEADER)
      lab.default_model = None  # the explicit choice supersedes --device
      await lab.refresh(write=False)
      out = {"config": lab.config_status(), "next": lab.next_step()}
      if identities:  # one device per server for now
        out["device_identity"] = next(iter(identities.values()))
      if dropped:
        out["note"] = f"The slot map of {', '.join(dropped)} is gone with the camera it was made for; map_camera makes a new one."
      if not keeps:
        out["next"] = (("This replaces a config that could not be read, so it may drop" if unreadable else "This drops or loosens")
                       + " task limits the person set: it waits for their approval. " + confirm_now())
      return out
    return await lab.shielded("configure_devices",
                              lambda: lab.logged("configure_devices",
                                                       {"devices": devices, "limits": limits,
                                                        **({"cameras": cameras} if cameras is not None else {}),
                                                        **({"auto_verify": auto_verify} if auto_verify is not None else {})}, body))

  @server.tool(annotations=PERSON_ONLY, meta=ASK_PERSON,
               description="For the person on site only: confirm this task's device config so that motion is allowed. Call it right away when the config or the session go-ahead needs confirming: "
                           "the person's approval of this call, in the client's dialog, is the confirmation, and a denial means not confirmed (then stop and ask what they want changed). "
                           "Never approve it for them; a client that shows no approval dialog on each call: ask in chat first. summary is config.confirm_summary exactly as lab_overview or configure_devices gave it, "
                           "so the person sees what they confirm. A real backend runs only from a config confirmed here. "
                           "It is also the person's go-ahead for this server session (lab_overview.session): a confirmed config is confirmed again.")
  async def confirm_config(summary: Annotated[str | None, Field(description="config.confirm_summary, exactly as given: the devices, where a real one is, and the task limits")] = None) -> dict[str, Any]:
    async def body():
      lab.require_not_stopped()
      await lab.refresh(write=False)
      if lab.problem is not None:
        raise LabError(lab.problem.code, lab.problem.message, lab.problem.hint)
      if lab.config is None or not lab.config.devices:
        raise LabError("nothing_to_confirm", "the config names no device yet.", "Call configure_devices first.")
      for e in lab.config.devices:  # never confirm limits that would loosen the description file's
        apply_limits(replace(device_spec(e.model, e.options), id=e.id), lab.config.limits)
        check_entry(e)
        if e.backend == "robot" and not e.options.get("robot_name"):
          raise LabError("robot_name_required",
                         f"the {e.id} entry names no robot_name, so another robot answering at that address would be "
                         "driven without anyone noticing.",
                         "Write the entry again with configure_devices: it reads the name from the robot. Then the person confirms it.")
      current = confirm_summary(lab.config)
      if summary != current:
        raise LabError("summary_mismatch",
                       "the summary does not match the config as it is now, so nothing was confirmed." if summary else
                       "confirm_config needs the config's summary, so the person sees what they confirm.",
                       f"The current summary is {current!r}: show it to the person and pass it exactly as summary.")
      lab.config.confirm("human")  # an unchanged, confirmed config too: stamped again, by the person, now
      lab.ws.write_config(lab.config.data(), HEADER)
      lab._go_ahead = lab.config.digest()  # the person's go-ahead for this session, on this config
      await lab.refresh(write=False)
      out = {"config": lab.config_status(), "next": lab.next_step()}
      if lab.adapter is not None and not lab.adapter.simulated:  # which robot the person confirmed, in the run log
        out["device_identity"] = await lab.record_identity()
      return out
    return await lab.shielded("confirm_config", lambda: lab.logged("confirm_config", {"summary": summary}, body))

  @server.tool(annotations=RELEASE,
               description="Let go of the robot without an emergency stop, keeping this server, the dashboard and the run log for the person to read: "
                           "the device's run ends (a robot is asked whether it did: run_ended_confirmed), its connection closes and the robot is free for another session. "
                           "Tips still on a pipette are noted, as a restart notes them (tips_left); the next load_layout asks the person. "
                           "Afterwards nothing moves (disconnected) until the person's go-ahead (confirm_config) and a load_layout. Not read-only: it ends the run.")
  async def disconnect() -> dict[str, Any]:
    return await lab.shielded("disconnect", lambda: lab.logged("disconnect", {}, lab.disconnect))

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
               description="Before writing a layout: the protocols in this workspace's .ahc/protocols/, the layouts saved in .ahc/layouts/, the layouts earlier runs here used, and last_deck: the deck the last run here ended with (volumes, used tip spots), as a layout ready to load. Only this workspace is searched.")
  async def find_layout_references() -> dict[str, Any]:
    def body():
      refs = lab.ws.references(model=lab.model())
      if refs["layouts"] or refs["run_layouts"]:
        refs["next"] = ("Base the new layout on these: in simulation one that puts the same labware types in the same "
                        "positions as one of them is judged by the simulator, any other waits for the person. load_layout "
                        "saves it in .ahc/layouts/; on real hardware only once the person confirms the deck.")
      elif refs["protocols"]:
        refs["next"] = ("Write the layout from these protocols. No layout here to match it against yet, so the person "
                        "confirms the first one, in simulation too (the deck check waits for their approval); load_layout "
                        "saves it in .ahc/layouts/ once they do.")
      elif lab.adapter is not None and lab.adapter.simulated:
        refs["next"] = ("No references in this workspace. In simulation start from lab_overview's "
                        "layout_format.example; the person confirms that layout before anything moves (the deck "
                        "check waits for their approval). On real hardware ask the person on site first.")
      else:
        refs["next"] = ("No references in this workspace: ask the person on site to describe the physical deck (each "
                        "slot's labware, the liquids and their volumes) before writing a layout. Before anything moves "
                        f"they check the deck against that layout: every slot's labware as listed, {ON_A_REAL_DECK}. "
                        "Physical verification help is a person for now; cameras later.")
      last = lab.last_deck()
      if last is not None:
        refs["last_deck"] = last
        refs["next"] = ("To continue from the last run's deck, load_layout last_deck.layout as is or edited (e.g. a fresh "
                        "plate added); the person confirms it at the deck check, since anything may have been touched "
                        "since. " + refs["next"])
      return refs
    return await lab.read(body)

  @server.tool(annotations=MOTION,
               description="Load a deck layout (labware, positions, starting liquids, aliases) and save it in .ahc/layouts/, or load a saved one by name. "
                           "See lab_overview for the format and find_layout_references for this workspace's earlier layouts. Resets the simulated device; "
                           "on a real robot it starts a new run and homes the robot, so hands and deck must be clear: say so in chat, in the same message before the dialog right ahead of it (the tips_mounted note or confirm_config), or ask in chat. "
                           "On real hardware, and in simulation when its labware types and positions match no layout saved or run in this task folder, a layout is saved only once the person confirms it.")
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
      lab.claim_robot()  # real hardware: one server per robot; refused (robot_in_use) while another holds it
      lab.require_tips_known()  # real hardware: a new run forgets the tips on the pipettes
      # A deck no layout saved or run in this folder matches (the built-in example, the agent's own, one written
      # from a protocol, or a reference with any labware changed, added, removed or moved): in simulation the person
      # confirms it, as on real hardware, told how it differs from the closest reference. A layout becomes a
      # reference (saved) only once the person confirmed it: on real hardware always, in simulation when it
      # matches none. A saved layout loaded by name is one.
      match = lab.reference_match(layout) if layout is not None else {}
      needs_person = layout is not None and lab.adapter.simulated and "matches" not in match
      hold = layout is not None and (needs_person or not lab.adapter.simulated)
      saved_as = name if layout is None else (name or lab.ws.next_layout_name())
      previous = lab.live_run()
      try:
        built = await lab.adapter.load_layout(chosen)
      except LabError as exc:  # a robot that does not answer: perhaps it answers at another address
        moved = await lab.renamed(exc)
        if moved is exc:
          raise
        raise moved from exc
      lab.started_run(previous)
      lab.interrupted = None  # a new run: what the person reported is in this layout
      found = lab.verification.layout_loaded()  # a failed liquid check described the old deck: the deck check says so
      # The device holds the new deck from here on: its gate exists before anything below can fail.
      lab.gate = Gate(layout=saved_as, needs_person=needs_person, pending_layout=layout if hold else None,
                      layout_ref=built, differs=differs_text(match) if layout is not None else None, found=found or None)
      lab.gate_changed("layout loaded")
      if not lab.adapter.simulated:
        lab.last_motion = time.time()  # the robot homed: captures from before show the head elsewhere
        lab.write_auto_state()
      built.record_placements()
      try:
        identity = await asyncio.to_thread(lab.adapter.identity)
      except Exception as exc:  # noqa: BLE001 - who the device is must never stop the load
        identity = {"error": f"{type(exc).__name__}: {exc}"}
      if identity:
        lab.record({"type": "device", "model": lab.spec.model, "backend": lab.adapter.backend,
                    **lab.endpoint(), **identity})
      if layout is not None and not hold:
        lab.ws.save_layout(saved_as, layout, lab.spec.model)
      out = {**built.summary(), "saved_as": saved_as, "path": str(lab.ws.layout_path(saved_as)), "gate": lab.gate.public()}
      if "matches" in match:
        out["matches_reference"] = match["matches"]
      elif "closest" in match:
        out.update(closest_reference=match["closest"], differences=match["differences"])
      if needs_person:
        out.update(needs_person=True, saved=False,
                   next=(f"{lab.gate.differs} So the person confirms this layout before anything moves: "
                         "verify(check='deck_matches_layout') gives ask_person for it. It is saved in .ahc/layouts/ once "
                         "they confirm it."))
      elif hold:
        out.update(saved=False, next=("On real hardware the person checks the deck before anything moves: "
                                      "verify(check='deck_matches_layout') gives ask_person for it. The layout is saved in "
                                      ".ahc/layouts/ once they confirm it."))
      else:
        out["next"] = "verify(check='deck_matches_layout') before anything moves."
      return out

    async def body():
      chosen = layout if layout is not None else (lab.ws.read_layout(name) if name else None)
      fresh = lab._robot_lock is None
      try:
        result = await lab.logged("load_layout", {"layout": chosen, "name": name}, lambda: load(chosen))
      except Exception:
        if fresh and lab.live_run() is None:  # no run was started: the robot is not held for it
          lab.release_robot()
        raise
      root = lab.adapter.view_root()
      if lab.view is not None and root is not None:
        lab.view.title = f"AHC · {lab.spec.title}"
        result = {**result, "live_view_url": await lab.view.show(root)}
      return result
    return await lab.act(body, "load_layout", {"layout": layout, "name": name}, plan_step, gated=False)

  @server.tool(annotations=READ_ONLY, description="Tips mounted, liquid in each container and fresh tips left (tips returned after use are counted apart), from the digital twin.")
  async def get_state(device: Annotated[str | None, Field(description="Component; omitted = all")] = None) -> dict[str, Any]:
    return await lab.read(lambda: lab.state(device))

  @server.tool(annotations=MOTION, description="Pick up tips on a component.")
  async def pick_up_tips(device: Device, plan_step: PlanStep,
                         tips: Annotated[list[str] | None, Field(description="e.g. ['tips:A1:H1'] or ['tips:col1']; omitted = the next fresh ones this pipette takes")] = None) -> dict[str, Any]:
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

  @server.tool(annotations=NOTE,
               description="Take a still from the task's cameras (config.cameras) and look at it: the images come back with this result. Call it around steps worth a look "
                           "(before a tip pick-up, an aspirate or a dispense) and with final=true when the plan's result is on the deck. cameras = names from config.cameras, "
                           "omitted = all, one after another. The full-size files stay in .ahc/runs/<run id>/<step folder>/ (plan_step picks the folder; without it, the step you are "
                           "on, else 0_preset; final=true: <N+1>_results), and the paths can go to verify(images=[...]) as evidence. Not for simulation (the camera shows the real deck). "
                           "Not motion: it also works while the emergency stop is engaged. Images stay on this computer. "
                           "A camera with a slot map (map_camera) also gets, per image: map (ok, unclear when the frame is out of focus or covered, needs_new_map when it no longer aligns to its reference), "
                           "frame_sharpness, alignment and slots: one entry per mapped slot with its box in the preview's pixels (image_size), level (unclear, slot: labware presence, type and liquid colour can be judged, "
                           "well: single wells can be resolved; tip for a tips:<component> region), sharpness, px_per_well and a crop file (<camera>_<HHMMSS>_slot<N>.jpg next to the image). "
                           "These are AHC's measurements: judge a slot or well only at the level it gives.")
  async def capture(label: Annotated[str, Field(description="What you want to see, one line, e.g. 'before the first aspirate'")],
                    plan_step: Annotated[int | None, Field(description="The step of your plan this capture belongs to; omitted = the step you are on")] = None,
                    cameras: Annotated[list[str] | None, Field(description="Camera names from config.cameras; omitted = all")] = None,
                    final: Annotated[bool, Field(description="The result of the whole plan: goes to the <N+1>_results folder")] = False) -> CallToolResult:
    blobs: list[tuple[str, str, bytes]] = []  # (camera, device, jpeg no wider than cams.MAX_WIDTH)

    async def captured() -> dict[str, Any]:
      return await lab.take_pictures(label, plan_step, cameras, final, blobs)

    async def body():
      lab.step_context = plan_step
      try:
        return await lab.logged("capture", {"label": label, "plan_step": plan_step, "cameras": cameras, "final": final}, captured)
      finally:
        lab.step_context = None
    result = await lab.run(body)
    content: list[Any] = [TextContent(type="text", text=json.dumps(result, indent=2))]
    for name, device, data in blobs:
      content.append(TextContent(type="text", text=f"{name} ({device}):"))
      content.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/jpeg"))
    return CallToolResult(content=content, structured_content=result)

  @server.tool(annotations=CONFIGURE,
               description="Give a camera a slot map: where each deck slot is in its picture, so captures are aligned and judged per slot. "
                           "Without slots: take a reference image (the deck clear, nothing in front of the lens, the pipette head parked away) and get it back with the OT-2 deck guide: seen from above slots 1-3 are the front row left to right, 4-6 and 7-9 the rows behind, then 10, 11 and the trash 12 in the back row. "
                           "Look at it and propose a box [x0, y0, x1, y1] for each slot you can see, in the pixels of the image you were given (image_size), hugging the labware's footprint (128 x 85 mm) by its guide lines and engraved number. "
                           "A box may reach past the frame (a plate cut off at the edge): give the whole footprint, AHC knows which part is visible. Leave out slots that are out of frame. A side view: view='side' (levels stop at slot). "
                           "A close-up region for tips: 'tips:<component>', e.g. 'tips:ot2.right'. With slots: they are written to the config (replacing the map's boxes) and drawn on the reference image, which comes back for you to check. "
                           "The config goes pending and the person confirms it with confirm_config; the deck is then checked again. The person can also drag the boxes on the dashboard. "
                           "Moves nothing; the reference is saved under .ahc/cameras/<camera>/. Not for simulation.")
  async def map_camera(camera: Annotated[str, Field(description="A camera name from config.cameras")],
                       slots: Annotated[dict[str, list[float]] | None, Field(description="Your proposal: {'2': [x0, y0, x1, y1], '5': [...]} for the image map_camera last gave you (else the camera's current reference); omitted = take a new reference image")] = None,
                       view: Annotated[Literal["top", "side"] | None, Field(description="top (a camera above the deck, the default) or side")] = None) -> CallToolResult:
    blobs: list[tuple[str, str, bytes]] = []

    async def mapped() -> dict[str, Any]:
      lab.poll_estop()
      await lab.refresh(write=False)
      lab.require_device()
      declared = {c.name: c for c in lab.config.cameras}
      if camera not in declared:
        raise LabError("unknown_camera", f"no camera {camera!r} in this config.",
                       f"Cameras: {', '.join(declared) or 'none'}; configure_devices(cameras=[...]) adds one.")
      entry, folder = declared[camera], lab.ws.cameras_dir / camera
      candidate, reference = folder / "candidate.jpg", folder / "reference.jpg"
      if slots is None:
        if lab.adapter.simulated:
          raise LabError("camera_not_in_simulation", "this server runs a simulation, and a camera shows the real deck.",
                         "Map a camera on real hardware (a robot backend the person confirmed).")
        cams.require_ffmpeg()
        lab.ws.require_task_folder()
        with tempfile.TemporaryDirectory(prefix="ahc-map-") as tmp:
          why = await asyncio.to_thread(cams.capture_frame, entry.device, Path(tmp) / "frame.jpg")
          if why is not None:
            raise LabError("capture_failed", f"{camera}: {why}",
                           "Check that the camera is plugged in, not used by another app, and allowed for this terminal.")
          preview = await asyncio.to_thread(cams.downscaled, Path(tmp) / "frame.jpg")
        gray = vision.read_gray_bytes(preview)
        if gray is None:
          raise LabError("capture_failed", f"{camera} gave an image that cannot be read.", "Try once more, or ask the person.")
        folder.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(candidate, preview)
        blobs.append((camera, entry.device, preview))
        out: dict[str, Any] = {"camera": camera, "device": entry.device, "image_size": [gray.shape[1], gray.shape[0]],
                               "saved_as": str(candidate), "deck_guide": DECK_GUIDE,
                               "next": f"Propose boxes: map_camera(camera='{camera}', slots={{'2': [x0, y0, x1, y1], ...}}) in the pixels of this image."}
        if entry.slot_map:
          out["current_map"] = {"sees": entry.sees(), "view": entry.slot_map.get("view", "top"), "slots": entry.slot_map["slots"],
                                "note": "A new map replaces it, and its streaks of agreed image judgments start again."}
        return out
      lab.require_not_stopped()
      source = candidate if candidate.is_file() else reference if entry.slot_map and reference.is_file() else None
      if source is None:
        raise LabError("no_reference_image", f"no reference image for {camera} to put boxes on.",
                       f"Call map_camera(camera='{camera}') without slots first: it takes the image and gives it back.")
      data = source.read_bytes()
      gray = vision.read_gray_bytes(data)
      if gray is None:
        raise LabError("no_reference_image", f"{source.name} cannot be read.", f"Call map_camera(camera='{camera}') without slots to take a new one.")
      size = [gray.shape[1], gray.shape[0]]
      try:
        slot_map = parse_slot_map({"reference": f"cameras/{camera}/reference.jpg", "size": size,
                                   "view": view or (entry.slot_map or {}).get("view", "top"),
                                   "sha256": hashlib.sha256(data).hexdigest(), "slots": slots}, camera)
      except LabError as exc:
        raise LabError("bad_slot_map", exc.message, "Propose {'<slot>': [x0, y0, x1, y1]} (pixels of the image, "
                       "x0 < x1, y0 < y1) per slot you can see, a slot '1'..'12' or 'tips:<component>'.") from exc
      outside = [k for k, (x0, y0, x1, y1) in slot_map["slots"].items() if x1 <= 0 or y1 <= 0 or x0 >= size[0] or y0 >= size[1]]
      if outside:
        raise LabError("box_outside_frame", f"no part of {', '.join(outside)} is inside the {size[0]} x {size[1]} image.",
                       "Leave out slots the camera does not see; a box may reach past the edge, but part of it must be in the frame.")
      if source == candidate:
        write_bytes_atomic(reference, data)
      cameras_now = [replace(c, slot_map=slot_map) if c.name == camera else c for c in lab.config.cameras]
      config = replace(lab.config, cameras=cameras_now)
      lab.ws.write_config(config.data(), HEADER)
      candidate.unlink(missing_ok=True)
      await lab.refresh(write=False)
      blobs.append((f"{camera} with your boxes", entry.device, vision.overlay(data, slot_map["slots"])))
      return {"camera": camera, "sees": slots_text(list(slot_map["slots"]), slot_map["view"]), "image_size": size,
              "slot_map": slot_map, "config": lab.config_status(),
              "next": "Check the drawn boxes against the deck. The config waits for the person: " + confirm_now()}

    async def body():
      return await lab.logged("map_camera", {"camera": camera, "slots": slots, "view": view}, mapped)
    result = await lab.shielded("map_camera", body)
    content: list[Any] = [TextContent(type="text", text=json.dumps(result, indent=2))]
    for name, device, data in blobs:
      content.append(TextContent(type="text", text=f"{name} ({device}):"))
      content.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/jpeg"))
    return CallToolResult(content=content, structured_content=result)

  @server.tool(annotations=NOTE,
               description="What AHC can observe of the real deck for this run, measured, and how far that goes. It is built from context sources: vision_context (the cameras: slot maps, alignment, quality gate) and instrument_context (the device's own sensors; the OT-2 has none). The digital twin is not a source: its volumes and tips are what observations are compared with. "
                           "Points (from the plan and the loaded layout): deck_matches_layout, every layout slot at level slot; liquid_present, the targets you declare with report_decision(vision_points=[...]) at level well; tips_mounted, 'tips:<pipette>' at level tip, which needs a camera region of that name or a tip sensor (the OT-2 has neither: it stays with the person). "
                           "It captures each mapped camera unless a capture newer than the last motion exists (recapture=true: always). Result: vision_context, instrument_context, points [{what, level, check, sources: [{source: 'vision:usb', level, fresh, covers}], status}], physical_world_confidence {by_check, by_source, all_confident}, suggestions, checked_at. "
                           "A point is covered when at least one source covers it at its level, unclear (blurred, covered or not captured: capture again; a camera that moved: map_camera again; its source entry says needs_new_map) or uncovered. No numeric score. by_check gives the same per check type with the sources and levels that cover; suggestions say what view or sensor would cover the rest. "
                           "Moves nothing; it saves captures like capture does. Real hardware only. With auto_verify in the config, run it (or capture) after the last motion and before verify: auto is on only when every point of every listed check type is covered, and only captures newer than the last motion count.")
  async def physical_world_context(recapture: Annotated[bool, Field(description="Capture again even if a capture newer than the last motion exists")] = False) -> dict[str, Any]:
    async def covered() -> dict[str, Any]:
      lab.poll_estop()
      await lab.refresh(write=False)
      lab.require_device()
      if lab.adapter.simulated:
        raise LabError("camera_not_in_simulation", "this server runs a simulation: a camera shows the real deck, and a simulated sensor is not a measurement.",
                       "Physical-world context is for real hardware (a robot backend the person confirmed).")
      points = lab.vision_points_now()
      if not points:
        return {**lab.physical_world([]), "note": "No verification points yet: they come from the plan (report_decision(plan=[...])) and the loaded layout (load_layout).",
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
      failures: list[dict[str, Any]] = []
      stale = [n for n, c in lab.vision_cameras().items() if recapture or c["analysis"] is None]
      if stale:
        failures = (await lab.take_pictures("physical world context", None, stale, False, [])).get("failures", [])
      result = lab.physical_world(points)
      at = time.strftime("%Y-%m-%dT%H:%M:%S")
      result["checked_at"] = at
      if failures:
        result["vision_context"]["failures"] = failures
      lab.vision_result = {"result": result, "at": at}
      lab.write_auto_state()
      try:
        lab.ws.state_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(lab.ws.state_dir / "physical_world.json", json.dumps(result, indent=2) + "\n")
      except OSError:  # the dashboard just shows the older one
        pass
      return result

    async def body():
      return await lab.logged("physical_world_context", {"recapture": recapture}, covered)
    return await lab.run(body)

  @server.tool(annotations=NOTE,
               description="Shadow mode, real hardware: while a check waits for the person (verify gave ask_person), say what the camera images show, before record_verdict. "
                           "Look at the crops capture saved (images are required: paths capture returned in this run). verdict: pass (the images show what the check asks), fail (they show a mismatch) or unclear (they cannot tell). "
                           "per_point: your judgment per verification point, [{'what': 'plate:B1', 'verdict': 'pass', 'note': 'red, about 100 uL'}]. "
                           "This passes nothing and moves nothing: the person's verdict decides. AHC records, per check type, whether your judgments agreed with theirs (an agreement streak; a disagreement or unclear resets it, a changed slot map resets all). "
                           "Not for the recovery checks after a stop, an unknown outcome or a restart (tips_mounted for every pipette): those stay with the person. "
                           "With auto_verify in the config, enough agreements in a row let verify hand a check type to you (agent_verdict).")
  async def propose_verdict(check_id: str, verdict: Literal["pass", "fail", "unclear"],
                            images: Annotated[list[str], Field(description="The captures you judged from: paths capture returned in this run (images[].path or slots[].crop)")],
                            per_point: Annotated[list[dict[str, str]] | None, Field(description="[{'what': ..., 'verdict': 'pass'|'fail'|'unclear', 'note': ...}]")] = None,
                            note: Annotated[str, Field(description="One line on what you saw")] = "") -> dict[str, Any]:
    async def proposed() -> dict[str, Any]:
      lab.require_not_stopped()
      await lab.refresh(write=False)
      lab.require_device()
      if lab.adapter.simulated:
        raise LabError("proposal_not_needed", "a simulated check is judged by the simulator; there is no person's verdict to learn from.",
                       "Use propose_verdict only on real hardware, for a check that waits for the person.")
      pending = lab.verification.pending.get(check_id)
      if pending is None:
        raise LabError("unknown_check_id", f"no check {check_id!r} waits for the person's verdict.",
                       "Run verify for the check first; propose_verdict, then record_verdict, use its check_id.")
      twin = (pending.get("evidence") or {}).get("digital_twin") or {}
      if pending["check"] == "tips_mounted" and "pipettes" in twin:
        raise LabError("recovery_check", "this is the check of every pipette after a stop, an unknown outcome or a restart: it stays with the person.",
                       "Go on with record_verdict (their dialog) as it is.")
      if not images:
        raise LabError("images_required", "a judgment from images needs the images.", "Pass the capture paths you looked at (images[].path or slots[].crop).")
      photos = lab.run_images(images)
      points = judged_points(per_point)
      lab.proposals[check_id] = {"verdict": verdict, "images": photos, "per_point": points, "note": note[:300],
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
      lab.record({"type": "vision", "event": "proposal", "check_id": check_id, "check": pending["check"], "verdict": verdict,
                  "images": photos, "per_point": points, "note": note[:300]})
      return {"recorded": True, "check_id": check_id, "check": pending["check"], "shadow": True,
              "next": "This passed nothing: the person decides. " + pass_now(check_id, "<its ask_person>")}

    async def body():
      try:
        return await proposed()
      except LabError as exc:
        lab.record_refusal("propose_verdict", {"check_id": check_id, "verdict": verdict}, exc)
        raise
    return await lab.run(body)

  @server.tool(annotations=NOTE,
               description="Auto-verify, real hardware: answer a check from camera images, only when verify marked it auto (auto: true; the person authorised it in the confirmed config, AHC measured that the cameras cover every point of the check, your earlier judgments agreed with the person's often enough, and nothing else ruled it out). "
                           "Look at the crops verify listed (images; paths capture returned in this run, taken since the last motion) and judge each point: pass (the images show what the check asks), fail (they show a mismatch) or unclear (hidden, blurred, off or you cannot tell). per_point: [{'what': 'slot:2', 'verdict': 'pass', 'note': 'reagent trough, red, about 5 mL'}]. note: one line on what you saw. "
                           "pass: AHC checks that auto still holds (no motion or stop, a capture and alignment still good, not paused) and then the check passes, judged_by 'agent (auto, cameras)', the images kept as its evidence; after a deck check the motion gate opens as for the person's pass. The person reviews every auto verdict on the dashboard. "
                           "fail or unclear (or a pass after auto stopped holding): nothing is recorded as a verdict; the check stays pending as the person's (result: auto false, auto_off, ask_person, your judgment shown to them), motion stays held, and they answer with record_verdict. Never a fail on your own. "
                           "Auto verdicts do not change the agreement streak. Always the person's: the recovery check of every pipette after a stop, an unknown outcome or a restart, the config and go-ahead, the homing go-ahead, the stop release.")
  async def agent_verdict(check_id: str, verdict: Literal["pass", "fail", "unclear"],
                          images: Annotated[list[str], Field(description="The captures you judged from: paths capture or verify gave in this run (images[].path or slots[].crop), taken since the last motion")],
                          per_point: Annotated[list[dict[str, str]] | None, Field(description="[{'what': ..., 'verdict': 'pass'|'fail'|'unclear', 'note': ...}]")] = None,
                          note: Annotated[str, Field(description="One line on what you saw")] = "") -> dict[str, Any]:
    async def judged() -> dict[str, Any]:
      lab.require_not_stopped()
      await lab.refresh(write=False)
      lab.require_device()
      if lab.adapter.simulated:
        raise LabError("auto_not_in_simulation", "a simulated check is judged by the simulator; cameras show the real deck.",
                       "Use agent_verdict only on real hardware, for a check verify marked auto.")
      pending = lab.verification.pending.get(check_id)
      if pending is None:
        lab.verification._waiting(check_id)  # check_superseded, or unknown_check_id
      if not pending.get("auto"):
        raise LabError("not_auto", f"verify did not mark check {check_id} auto, so the person judges it.",
                       f"Report waiting_for='person' and go on with record_verdict (their dialog); its ask_person is in verify's result. (verify's auto_off says why the cameras do not answer it.)")
      lab.require_current_deck_check(check_id, pending)
      if not images:
        raise LabError("images_required", "a judgment from images needs the images.", "Pass the crops verify listed (images).")
      photos = lab.run_images(images)
      points = judged_points(per_point)
      judgment = {"verdict": verdict, "images": photos, "per_point": points, "note": note[:300]}
      info = lab.auto_checks.get(check_id) or {}
      assess = lab.auto_assess(pending["check"], info.get("targets"))
      if assess["why"] is not None:  # a motion, a stop, a blurred capture, a pause since verify: it is the person's
        return lab.hand_back(check_id, assess["why"], judgment)
      if verdict != "pass" or any(p["verdict"] != "pass" for p in points):
        said = verdict if verdict != "pass" else "a point that is not a pass"
        return lab.hand_back(check_id, f"you judged {said}: the person decides, and nothing is recorded as a verdict", judgment)
      old = [p for p in photos if Path(p).stat().st_mtime <= lab.last_motion]
      if old:
        raise LabError("stale_images", f"{len(old)} of the images were taken before the robot last moved, so they show an older deck.",
                       "Capture again (physical_world_context or capture), judge those crops, then agent_verdict again; the check still waits.")
      result = lab.verification.agent_pass(check_id, note[:300], photos, points)
      lab.auto_checks.pop(check_id, None)
      lab.proposals.pop(check_id, None)
      gate = lab.gate
      if result["check"] == "deck_matches_layout" and gate is not None and gate.check_id == check_id:
        gate.apply(check_id, "pass")
        lab.gate_changed("the agent judged pass from camera images (auto-verify)")
        result = {**result, "gate": gate.public()}
        if gate.pending_layout is not None:
          result["layout_note"] = "The layout is saved in .ahc/layouts/ only once the person confirms a deck check."
      elif result["check"] == "tips_mounted":
        lab.tips_checked()
      lab.record({"type": "vision", "event": "auto_verdict", "check_id": check_id, "check": result["check"], "verdict": "pass",
                  "images": photos, "per_point": points, "note": note[:300]})
      auto.append_verdict(lab.ws, {"check_id": check_id, "check": result["check"], "images": photos, "per_point": points, "note": note[:300]})
      lab.write_auto_state()
      return {**result, "review": "Logged with its images and listed on the dashboard for the person to review.",
              "next": lab.next_step()}

    async def body():
      try:
        return await judged()
      except LabError as exc:
        lab.record_refusal("agent_verdict", {"check_id": check_id, "verdict": verdict}, exc)
        raise
    return await lab.run(body)

  @server.tool(annotations=READ_ONLY, description="Physical-world checks this server can gather evidence for.")
  async def list_checks() -> dict[str, Any]:
    def body():
      judged = ("depends on the device; configure it first" if lab.adapter is None
                else "simulation" if lab.adapter.simulated else "human (placeholder)")
      out: dict[str, Any] = {"checks": CHECKS, "judged_by": judged}
      photographed: dict[str, dict[str, Any]] = {}  # this run's checks that carry photos from capture
      for e in lab.runlog.entries if lab.runlog is not None else []:
        if e.get("type") != "verification":
          continue
        camera = (e.get("evidence") or {}).get("camera")
        if isinstance(camera, dict) and camera.get("images"):
          photographed[e["check_id"]] = {"check_id": e["check_id"], "check": e.get("check"), "images": camera["images"]}
        if e.get("check_id") in photographed:
          photographed[e["check_id"]].update(verdict=e.get("verdict"), judged_by=e.get("judged_by"))
      if photographed:
        out["checks_with_images"] = list(photographed.values())
      return out
    return await lab.read(body)

  @server.tool(annotations=GATE,
               description="Gather evidence for a check (digital twin, sensors, camera) and return a verdict. deck_matches_layout also opens or closes the motion gate after load_layout, "
                           "so this is not read-only. On real hardware, and in simulation for a layout no reference matches, the verdict waits for the person: the result is pending with ask_person, one plain sentence of what they confirm, "
                           "which you pass as the note of record_verdict(check_id, verdict='pass') right away; while it waits, "
                           "verify(deck_matches_layout) returns that same check. While a tips_mounted or liquid_present check waits for the person nothing moves (check_pending; "
                           "verifying the same check again replaces it); once they fail one, a tips check leaves only drop_tips, a liquid check stops pick-ups and liquid (check_failed). "
                           "volumes_match compares expected volumes with the digital twin on every backend: what was sent, not measured. "
                           "tips_mounted with expect='none' and no device checks every pipette at once: on real hardware the person's pass that the pipettes are empty, which load_layout waits for "
                           "when tips may be on that the robot no longer knows (tips_unconfirmed). "
                           "Auto-verify (the config lists the check type, real hardware): when AHC finds the cameras cover every point of the check with captures newer than the last motion and your earlier judgments agreed with the person's often enough, "
                           "the result is pending with auto: true, points, crops (images) and ask_agent instead of ask_person: you judge it with agent_verdict. Otherwise it is the person's as above, with auto_off saying which condition failed. "
                           "The recovery check of every pipette is never auto.")
  async def verify(check: Annotated[str, Field(description="tips_mounted, liquid_present, deck_matches_layout or volumes_match")],
                   plan_step: PlanStep,
                   device: Annotated[str | None, Field(description="Component, for tips_mounted; omitted with expect='none': every pipette")] = None,
                   targets: Annotated[list[str] | None, Field(description="For liquid_present; any target form, e.g. plate:col2")] = None,
                   expect: Annotated[Literal["mounted", "none"], Field(description="For tips_mounted")] = "mounted",
                   min_volume_ul: float = 0.0,
                   expected: Annotated[dict[str, float] | None, Field(description="For volumes_match: {target: uL}, e.g. {'plate:col2': 100}; any target form")] = None,
                   tolerance_ul: Annotated[float, Field(description="For volumes_match: the difference allowed, uL")] = 0.5,
                   images: Annotated[list[str] | None, Field(description="Photos as evidence: paths capture returned in this run (images[].path)")] = None) -> dict[str, Any]:
    async def checked():
      lab.require_not_stopped()
      await lab.refresh(write=False)
      lab.require_device()
      lab.plan.check(plan_step)
      photos = lab.run_images(images) if images else None
      last = lab.last_step_starts(plan_step)
      deck = check == "deck_matches_layout" and lab.gate is not None
      waiting = lab.gate.waiting_for_person() if deck else None
      if waiting is not None and waiting in lab.verification.pending:  # nobody has answered yet
        pending = lab.verification.pending[waiting]
        if pending.get("auto"):  # the agent's to judge: if auto no longer applies, the person's
          info = lab.auto_checks.get(waiting, {})
          assess = lab.auto_assess(check, info.get("targets"))
          result = lab.auto_view(pending, assess["points"]) if assess["why"] is None else lab.hand_back(waiting, assess["why"])
          result = {**result, "gate": lab.gate.public(),
                    "repeat": "This deck check already waits for a verdict; no new check was made."}
          return lab.plan_note(lab.tips_warning(result), last)
        result = {**pending, "gate": lab.gate.public(),
                  "repeat": "This deck check already waits for the person's verdict; no new check was made."}
        if lab.config.auto_verify and not lab.adapter.simulated:
          assess = lab.auto_assess(check)
          if assess["why"] is None:  # the cameras cover it now (a capture since): it becomes the agent's to judge
            lab.verification.mark_auto(waiting)
            lab.gate.auto = True
            lab.auto_checks[waiting] = {"check": check, "device": None, "targets": None, "expect": "mounted"}
            result = {**lab.auto_view(lab.verification.pending[waiting], assess["points"]), "gate": lab.gate.public(),
                      "repeat": "This deck check was waiting for the person; the cameras cover it now, so you judge it."}
          else:
            result["auto_off"] = assess["why"]
        return lab.plan_note(lab.tips_warning(result), last)
      # The deck check tells the person how the layout differs from the references, and what they found before it.
      told = " ".join(t for t in (lab.gate.differs, lab.gate.found) if t) if deck else ""
      assess = None  # real hardware with auto_verify in the config: is this check answered by the cameras now?
      if check != "volumes_match" and lab.config.auto_verify and not lab.adapter.simulated:
        assess = lab.auto_assess(check, targets, recovery=check == "tips_mounted" and device is None)
      lab.step_context = plan_step
      try:
        result = await lab.verification.verify(check, device, targets, expect, min_volume_ul,
                                               person=deck and lab.gate.needs_person, expected=expected,
                                               tolerance_ul=tolerance_ul, differs=told or None, images=photos,
                                               auto=assess is not None and assess["why"] is None)
      finally:
        lab.step_context = None
      if deck:
        lab.gate.apply(result["check_id"], result["verdict"])
        lab.gate.auto = bool(result.get("auto"))
        lab.gate_changed(f"deck check {result['verdict']}")
        result = {**result, "gate": lab.gate.public()}  # a copy: the pending check is stored as it was
      if result.get("auto"):
        lab.auto_checks[result["check_id"]] = {"check": check, "device": device, "targets": targets, "expect": expect}
        gate = result.pop("gate", None)
        result = lab.auto_view(result, assess["points"])
        if gate is not None:
          result["gate"] = gate
      elif assess is not None and result.get("verdict") == "pending":
        result = {**result, "auto_off": assess["why"]}
      lab.write_auto_state()
      return lab.plan_note(lab.tips_warning(result), last)

    async def body():
      try:
        return await checked()
      except LabError as exc:
        lab.record_refusal("verify", {"check": check, "device": device, "targets": targets, "expected": expected,
                                        **({"images": images} if images else {})}, exc)
        raise
    return await lab.run(body)

  @server.tool(annotations=PERSON_ONLY, meta=ASK_PERSON,
               description="For the person only: record pass or fail for a check that waits for them (on real hardware every check but volumes_match and a check with auto: true, which agent_verdict answers; in simulation, a layout no reference matches). "
                           "Call it right away with verdict='pass' and note=verify's ask_person: the person's approval of this call, in the client's dialog, is their confirmation. "
                           "A denied call means not confirmed: stop, ask the person what they see, then record verdict='fail' with their words as the note (this call asks them too). "
                           "Never approve it for them or invent their words; a client that shows no approval dialog on each call: ask in chat first.")
  async def record_verdict(check_id: str, verdict: Literal["pass", "fail"], note: str = "") -> dict[str, Any]:
    async def recorded():
      lab.require_not_stopped()
      await lab.refresh(write=False)
      lab.require_device()
      gate, pending = lab.gate, lab.verification.pending.get(check_id)
      lab.require_current_deck_check(check_id, pending)
      result = lab.verification.record_verdict(check_id, verdict, note)
      lab.auto_checks.pop(check_id, None)  # the person answered an auto check themselves
      proposal = lab.proposals.pop(check_id, None)
      if proposal is not None and not lab.adapter.simulated:  # the agent judged from images first: compare with the person
        kept = VisionRecord(lab.ws).compare(lab.vision_digest(), result["check"], proposal["verdict"], verdict, check_id)
        lab.record({"type": "vision", "event": "agreement", "check_id": check_id, "check": result["check"],
                    "agent": proposal["verdict"], "person": verdict, "agree_streak": kept["agree_streak"]})
        result = {**result, "vision": {"agent": proposal["verdict"], "agreed": proposal["verdict"] == verdict,
                                       "agree_streak": kept["agree_streak"]}}
      if result["check"] == "deck_matches_layout" and gate is not None and gate.check_id == check_id:
        gate.apply(check_id, verdict)
        if verdict == "pass" and gate.pending_layout is not None:  # confirmed: now a reference for this folder
          path = lab.ws.save_layout(gate.layout, gate.pending_layout, lab.spec.model)
          gate.pending_layout, gate.needs_person = None, False
          result = {**result, "saved_as": gate.layout, "path": str(path)}
        lab.gate_changed(f"the person recorded {verdict}")
        result = {**result, "gate": gate.public()}
        if verdict == "fail":
          result["next"] = ("Ask the person what differs, then load the corrected layout (or they fix the deck and you "
                            "verify again); the new deck check goes to them again.")
      elif verdict == "fail":
        result["next"] = (f"The person found {result['check']} not as expected: stop and go on only as they say.")
      every = result["check"] == "tips_mounted" and "pipettes" in (result["evidence"]["digital_twin"] or {})
      if result["check"] == "tips_mounted" and not lab.adapter.simulated:
        if every and verdict == "pass":  # the one check for every pipette: what load_layout waits for
          lab.pipettes_empty()
        else:  # tips the person found go on disk, and come off once a drop or a later check clears them
          lab.tips_checked()
        if every and verdict == "fail":
          result["next"] = ("The person still sees tips on the pipettes (or could not check): they remove them by hand; "
                            "then verify(check='tips_mounted', expect='none') again without device and record_verdict "
                            "with its ask_person. load_layout stays refused until they pass it.")
      held = lab.verification.held_after(check_id)  # what their failed tips or liquid check holds back now
      if held:
        result["next"] = f"{result['next']} {held}"
      return {**result, "next": result.get("next") or lab.next_step()}

    async def body():
      try:
        return await recorded()
      except LabError as exc:
        lab.record_refusal("record_verdict", {"check_id": check_id, "verdict": verdict}, exc)
        raise
    return await lab.run(body)

  @server.tool(annotations=NOTE,
               description="Tell the person watching what you decided and why, in one line each. First, before load_layout or any action: your plan, as short concrete steps in `plan` "
                           "(not in the decision text); every load_layout, verify and action call then names its step with plan_step, and the dashboard ticks a step once you move on. "
                           "Also report a changed plan, a refusal that changes what you do, and waiting for the person's approval (waiting_for='person', just before the confirm_config or record_verdict call they approve; with current_step if that is a step of its own). "
                           "When the last step is done, report the result with plan_done=true. With cameras that have slot maps, vision_points declares what they must see for your checks (liquid_present targets at level well); physical_world_context then says what they cover. "
                           "Shown on the run dashboard and kept in this task's run log (so it writes .ahc/runs).")
  async def report_decision(decision: Annotated[str, Field(description="What you decided to do now, one line")],
                            why: Annotated[str | None, Field(description="Why, one line")] = None,
                            plan: Plan = None,
                            current_step: Annotated[int | None, Field(description="A step that starts without a call of its own (e.g. waiting for the person); calls name theirs with plan_step")] = None,
                            waiting_for: Annotated[Literal["person"] | None, Field(description="Set while you wait for the person's approval")] = None,
                            plan_done: Annotated[bool, Field(description="The plan is finished: this report gives the result")] = False,
                            vision_points: Annotated[list[dict[str, str]] | None, Field(description="With the plan: what the cameras must see for your checks, e.g. [{'what': 'plate:B1:B8', 'level': 'well', 'check': 'liquid_present'}]. what: a target, 'slot:<n>' or 'tips:<component>'; level: slot, well or tip. They add to the points AHC derives from the loaded layout; a new plan starts the list again")] = None) -> dict[str, Any]:
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
      declared = None
      if vision_points is not None:
        declared = vpoints.parse_points(vision_points)
        if lab.loaded_layout() is not None:
          vpoints.check_points(declared, lab.loaded_layout())
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
      if declared is not None:
        entry["vision_points"] = declared
      lab.record(entry)
      if plan is not None:  # a new plan: the agent's points start again
        lab.vision_points = []
      if declared:
        lab.vision_points = vpoints.merged(lab.vision_points, declared)
      result: dict[str, Any] = {"recorded": True, "shown": "on the run dashboard (ahc-dashboard) and in the run log"}
      if plan_done:
        result["timing"] = run_timing(lab.runlog.written)
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
  parser.add_argument("--backend", choices=["sim", "config"], default=None,
                      help="sim: force simulation (also AHC_BACKEND=sim); config: follow the task folder's config, "
                           "as without --backend (AHC_BACKEND=sim still forces simulation)")
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
    server.lab.handle_signals = True  # streamable-http runs under uvicorn, which ends its lifespan on a signal itself
    server.run("stdio")
  else:
    server.run("streamable-http", host="127.0.0.1", port=args.http_port)


if __name__ == "__main__":
  main()
