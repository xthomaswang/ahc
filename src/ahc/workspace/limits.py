"""Task limits: a workspace may tighten the description file's limits, never loosen or replace them.

    limits:
      starlet.pip:                          # <device id>.<component>
        volume_ul: {max: 200}               # basic limit
        flow_rate_ul_s: {min: 5, max: 100}
        aspirate.settling_time_s: {max: 2}  # specialized parameter: <op>.<name>

Only `min` and `max` of numeric parameters can be set, and each must lie inside the description
file's range. A tightened parameter the call leaves out would fall to the device's own default,
which may sit outside the task range, so the capability layer then needs an explicit value (or
uses the description file's default when that lies inside).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from ahc.core.errors import LabError
from ahc.devices.spec import DeviceSpec, Param


def _refuse(code: str, message: str, hint: str) -> LabError:
  return LabError(code, message, hint)


def _number(value: Any, where: str) -> float:
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
    raise _refuse("config_invalid", f"{where} must be a finite number, got {value!r}.", "Use a number.")
  return value


def tighten_param(base: Param, bound: Any, where: str) -> Param:
  if base.type not in ("number", "integer"):
    raise _refuse("config_invalid", f"{where}: {base.type} parameters cannot take task limits.",
                  "Only numeric parameters can be limited.")
  if not isinstance(bound, dict) or not bound or set(bound) - {"min", "max"}:
    raise _refuse("config_invalid", f"{where}: a task limit sets only min and/or max, got {bound!r}.",
                  "Write e.g. {max: 200}. Defaults, types and sources come from the description file only.")
  lo = _number(bound["min"], f"{where}.min") if "min" in bound else base.min
  hi = _number(bound["max"], f"{where}.max") if "max" in bound else base.max
  device = f"the device allows {base.min}..{base.max}"
  if base.min is not None and lo is not None and lo < base.min:
    raise _refuse("limit_loosened", f"{where}: min {lo} is below the description file's {base.min}.",
                  f"Task limits can only tighten: {device}.")
  if base.max is not None and hi is not None and hi > base.max:
    raise _refuse("limit_loosened", f"{where}: max {hi} is above the description file's {base.max}.",
                  f"Task limits can only tighten: {device}.")
  if lo is not None and hi is not None and lo > hi:
    raise _refuse("config_invalid", f"{where}: min {lo} is above max {hi}.", "Give min <= max.")
  source = f"task limit in .ahc/config.yaml; {device}" + (f" ({base.source})" if base.source else "")
  return replace(base, min=lo, max=hi, source=source, tightened=True)


def apply_limits(spec: DeviceSpec, limits: dict[str, Any]) -> DeviceSpec:
  """The spec with the workspace's task limits applied; refuses anything that would loosen."""
  components = dict(spec.components)
  for address, params in (limits or {}).items():
    prefix, _, name = str(address).partition(".")
    if prefix != spec.prefix or name not in components:
      known = ", ".join(f"{spec.prefix}.{c}" for c in components)
      raise _refuse("config_invalid", f"limits: {address!r} is not a component of this task's device.",
                    f"Components: {known}.")
    if not isinstance(params, dict):
      raise _refuse("config_invalid", f"limits.{address} must map parameters to bounds.", "Write e.g. volume_ul: {max: 200}.")
    comp = components[name]
    basic = dict(comp.limits)
    specialized = {op: dict(ps) for op, ps in comp.specialized.items()}
    for key, bound in params.items():
      where = f"limits.{address}.{key}"
      op, _, pname = str(key).rpartition(".")
      if op:
        base = specialized.get(op, {}).get(pname)
        if base is None:
          raise _refuse("config_invalid", f"{where}: {address} has no specialized parameter {pname!r} for {op}.",
                        "Use get_params(device, op) to see the specialized parameters.")
        specialized[op][pname] = tighten_param(base, bound, where)
      else:
        base = basic.get(pname)
        if base is None:
          raise _refuse("config_invalid", f"{where}: {address} has no limit named {pname!r}.",
                        f"Basic limits: {', '.join(basic)}; specialized: <op>.<name>.")
        basic[pname] = tighten_param(base, bound, where)
    # Parameters that move liquid through the tip (a native mix, a pre-wet) stay within a tightened
    # volume cap. Leaving them out still means "none", so they are capped, not marked as needing a value.
    for op, ps in specialized.items():
      for pname, param in ps.items():
        cap = basic.get(param.follows) if param.follows else None
        if cap is not None and cap.tightened and cap.max is not None and (param.max is None or cap.max < param.max):
          ps[pname] = replace(param, max=cap.max,
                              source=f"capped by the task limit on {param.follows} ({cap.max}); device allows "
                                     f"{param.min}..{param.max}" + (f" ({param.source})" if param.source else ""))
    components[name] = replace(comp, limits=basic, specialized=specialized)
  return replace(spec, components=components)


def keeps_limits(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
  """Every task limit in `old` is still there in `new`, at least as strict."""
  for address, params in (old or {}).items():
    kept = (new or {}).get(address)
    if not isinstance(kept, dict) or not isinstance(params, dict):
      return False
    for key, bound in params.items():
      now = kept.get(key)
      if not isinstance(now, dict) or not isinstance(bound, dict):
        return False
      if "min" in bound and (now.get("min") is None or now["min"] < bound["min"]):
        return False
      if "max" in bound and (now.get("max") is None or now["max"] > bound["max"]):
        return False
  return True
