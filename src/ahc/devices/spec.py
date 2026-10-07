"""Device description files: YAML front matter for the server, Markdown prose for the agent.

The front matter is the single source of every limit. The server builds its asserts from it and
returns the same numbers next to each parameter, so documentation and enforcement cannot drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import yaml

from ahc.core.errors import LabError

# Description files ship inside the package, one per model: ahc/devices/<brand>/<model>.md.
DEVICES_PACKAGE = "ahc.devices"

LIQUID_HANDLING_OPS = ("pick_up_tips", "aspirate", "dispense", "mix", "drop_tips")
BASIC_LIMITS = ("volume_ul", "flow_rate_ul_s", "liquid_height_mm", "mix_repetitions")
PARAM_TYPES = ("boolean", "number", "integer")


@dataclass(frozen=True)
class Param:
  """One parameter with its limits, as declared in a description file."""

  name: str
  type: str
  min: float | None = None
  max: float | None = None
  default: Any = None
  doc: str = ""
  source: str | None = None
  tightened: bool = False  # a workspace task limit narrowed min/max
  follows: str | None = None  # liquid moved through the tip: a task limit on this basic limit caps it too

  def public(self) -> dict[str, Any]:
    out: dict[str, Any] = {"type": self.type}
    for key in ("min", "max", "default"):
      if getattr(self, key) is not None:
        out[key] = getattr(self, key)
    if self.doc:
      out["doc"] = self.doc
    if self.source:
      out["source"] = self.source
    if self.tightened:
      out["task_limit"] = True
    return out

  def covers_default(self) -> bool:
    """The description file's default lies inside this (possibly tightened) range."""
    if self.default is None or isinstance(self.default, bool):
      return False
    return ((self.min is None or self.default >= self.min) and (self.max is None or self.default <= self.max))

  def check(self, value: Any) -> Any:
    """Return the value if it is allowed, else refuse with the real bound."""
    if self.type == "boolean":
      if not isinstance(value, bool):
        raise LabError("bad_type", f"{self.name} must be true or false, got {value!r}.")
      return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
      raise LabError("bad_type", f"{self.name} must be a finite number, got {value!r}.")
    if self.type == "integer" and float(value) != int(value):
      raise LabError("bad_type", f"{self.name} must be a whole number, got {value!r}.")
    if (self.min is not None and value < self.min) or (self.max is not None and value > self.max):
      raise LabError(
        "out_of_range",
        f"{self.name}={value} is outside {self.min}..{self.max}.",
        f"Use a value between {self.min} and {self.max}"
        + (f" ({self.source})." if self.source else "."),
      )
    return int(value) if self.type == "integer" else float(value)


@dataclass(frozen=True)
class Component:
  name: str
  title: str
  capabilities: tuple[str, ...]
  channels: int
  shared_container: bool
  equal_volumes: bool
  limits: dict[str, Param]
  specialized: dict[str, dict[str, Param]]
  translations: dict[str, str]
  note: str = ""

  def public(self) -> dict[str, Any]:
    out: dict[str, Any] = {"title": self.title, "capabilities": list(self.capabilities)}
    if self.capabilities:
      out.update(
        channels=self.channels,
        shared_container=self.shared_container,
        equal_volumes=self.equal_volumes,
        limits={k: p.public() for k, p in self.limits.items()},
        specialized_ops=sorted(op for op, params in self.specialized.items() if params),
        translations=self.translations,
      )
    if self.note:
      out["note"] = self.note
    return out


@dataclass(frozen=True)
class DeviceSpec:
  model: str
  brand: str
  title: str
  backends: dict[str, str]
  layout: dict[str, Any]
  components: dict[str, Component]
  guide: str
  path: Any  # pathlib.Path or importlib.resources Traversable; both have read_text()
  raw: dict[str, Any] = field(repr=False, default_factory=dict)
  id: str | None = None  # the workspace config's device id; addresses are <id>.<component>

  @property
  def short_name(self) -> str:
    return self.model.split(".", 1)[1]

  @property
  def prefix(self) -> str:
    return self.id or self.short_name

  def component(self, address: str) -> Component:
    """Resolve `starlet.pip` or `pip` to a component."""
    name = address.split(".", 1)[1] if address.startswith(self.prefix + ".") else address
    if name not in self.components:
      known = ", ".join(f"{self.prefix}.{c}" for c in self.components)
      raise LabError("unknown_component", f"{address!r} is not a component of {self.model}.",
                     f"Use one of: {known}.")
    return self.components[name]


def _param(name: str, raw: dict[str, Any], where: str) -> Param:
  kind = raw.get("type", "number")
  if kind not in PARAM_TYPES:
    raise ValueError(f"{where}: {name} has unknown type {kind!r}")
  follows = raw.get("follows")
  if follows is not None and follows not in BASIC_LIMITS:
    raise ValueError(f"{where}: {name} follows unknown limit {follows!r}")
  return Param(name=name, type=kind, min=raw.get("min"), max=raw.get("max"),
               default=raw.get("default"), doc=raw.get("doc", ""), source=raw.get("source"), follows=follows)


def _component(name: str, raw: dict[str, Any], where: str) -> Component:
  caps = tuple(raw.get("capabilities", []))
  limits = {k: _param(k, v, f"{where}.{name}.limits") for k, v in raw.get("limits", {}).items()}
  if "liquid_handling" in caps:
    missing = [k for k in BASIC_LIMITS if k not in limits]
    if missing:
      raise ValueError(f"{where}.{name}: liquid_handling needs limits for {missing}")
  specialized: dict[str, dict[str, Param]] = {}
  for op, params in (raw.get("specialized") or {}).items():
    if op not in LIQUID_HANDLING_OPS:
      raise ValueError(f"{where}.{name}: specialized op {op!r} is not a liquid-handling op")
    specialized[op] = {k: _param(k, v, f"{where}.{name}.{op}") for k, v in (params or {}).items()}
  return Component(
    name=name, title=raw.get("title", name), capabilities=caps, channels=int(raw.get("channels", 0)),
    shared_container=bool(raw.get("shared_container", False)),
    equal_volumes=bool(raw.get("equal_volumes", False)), limits=limits, specialized=specialized,
    translations=dict(raw.get("translations") or {}), note=raw.get("note", ""),
  )


def parse_device(path) -> DeviceSpec:
  text = path.read_text()
  if not text.startswith("---\n"):
    raise ValueError(f"{path}: no YAML front matter")
  _, front, guide = text.split("---\n", 2)
  raw = yaml.safe_load(front)
  components = {n: _component(n, c, str(path)) for n, c in raw["components"].items()}
  return DeviceSpec(model=raw["model"], brand=raw["brand"], title=raw["title"],
                    backends=raw.get("backends", {}), layout=raw.get("layout", {}),
                    components=components, guide=guide.strip(), path=path, raw=raw)


def known_models() -> list[str]:
  """Every model with a description file in the package, as brand.model."""
  root = resources.files(DEVICES_PACKAGE)
  return sorted(f"{brand.name}.{f.name[:-3]}" for brand in root.iterdir() if brand.is_dir()
                for f in brand.iterdir() if f.name.endswith(".md"))


def load_device(model: str) -> DeviceSpec:
  brand, _, name = model.partition(".")
  path = resources.files(DEVICES_PACKAGE).joinpath(brand, f"{name}.md")
  if not brand or not name or not path.is_file():
    raise ValueError(f"no description file for {model!r}; known: {known_models()}")
  spec = parse_device(path)
  if spec.model != model:
    raise ValueError(f"{path} declares model {spec.model!r}, expected {model!r}")
  return spec
