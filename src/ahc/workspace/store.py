"""The workspace: one task folder, with everything AHC keeps for that task under `<folder>/.ahc/`.

    .ahc/config.yaml   devices, task limits, confirmation, base simulator versions used
    .ahc/layouts/      layouts the agent wrote (saved by load_layout)
    .ahc/protocols/    protocols the agent consults when writing a layout
    .ahc/runs/         run logs
    .ahc/sim/          this task's simulator settings and state

Nothing is created until a tool needs to write: a globally installed plugin starts the server in
every Claude Code session, so merely starting it must leave the folder untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from ahc.core.errors import LabError

SUBDIRS = ("layouts", "protocols", "runs", "sim")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def resolve_workspace(arg: str | os.PathLike | None = None) -> Path:
  """--workspace, else AHC_WORKSPACE, else the folder the server was started in."""
  chosen = arg or os.environ.get("AHC_WORKSPACE") or os.getcwd()
  return Path(chosen).expanduser().resolve()


class Workspace:
  def __init__(self, root: Path):
    self.root = Path(root)
    self.dir = self.root / ".ahc"

  @property
  def config_path(self) -> Path:
    return self.dir / "config.yaml"

  @property
  def layouts_dir(self) -> Path:
    return self.dir / "layouts"

  @property
  def protocols_dir(self) -> Path:
    return self.dir / "protocols"

  @property
  def runs_dir(self) -> Path:
    return self.dir / "runs"

  @property
  def sim_dir(self) -> Path:
    return self.dir / "sim"

  def ensure(self) -> None:
    if not self.root.is_dir():
      raise LabError("no_workspace", f"workspace folder {self.root} does not exist.",
                     "Start the server in the task folder, or pass --workspace / AHC_WORKSPACE.")
    for sub in SUBDIRS:
      (self.dir / sub).mkdir(parents=True, exist_ok=True)

  # -- config file ----------------------------------------------------------------------------

  def read_config_text(self) -> str | None:
    return self.config_path.read_text() if self.config_path.is_file() else None

  def write_config(self, data: dict[str, Any], header: str) -> None:
    self.ensure()
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    self.config_path.write_text(header.rstrip() + "\n" + body)

  # -- layouts --------------------------------------------------------------------------------

  def layout_path(self, name: str) -> Path:
    if not NAME.fullmatch(name):
      raise LabError("bad_name", f"{name!r} is not a usable layout name.",
                     "Use letters, digits, '-', '_' or '.', starting with a letter or digit.")
    return self.layouts_dir / f"{name}.yaml"

  def save_layout(self, name: str, layout: dict[str, Any], model: str) -> Path:
    self.ensure()
    path = self.layout_path(name)
    header = (f"# Layout for {model}, saved by AHC at {time.strftime('%Y-%m-%dT%H:%M:%S')}.\n"
              "# Load it again with load_layout(name=...); the deck check confirms it before anything moves.\n")
    path.write_text(header + yaml.safe_dump(layout, sort_keys=False, allow_unicode=True))
    return path

  def read_layout(self, name: str) -> dict[str, Any]:
    path = self.layout_path(name)
    if not path.is_file():
      known = sorted(p.stem for p in self.layouts_dir.glob("*.yaml")) if self.layouts_dir.is_dir() else []
      raise LabError("unknown_layout", f"no saved layout {name!r} in {self.layouts_dir}.",
                     f"Saved layouts: {', '.join(known) or 'none'}. A layout waiting for the person's confirmation "
                     "is saved only once they confirm it; load it again with layout=.")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
      raise LabError("bad_layout", f"{path} does not hold a layout mapping.")
    return data

  def next_layout_name(self) -> str:
    taken = {p.stem for p in self.layouts_dir.glob("*.yaml")} if self.layouts_dir.is_dir() else set()
    n = 1
    while f"layout-{n}" in taken:
      n += 1
    return f"layout-{n}"

  # -- references for writing a layout ------------------------------------------------------------

  def has_reference(self) -> bool:
    """Anything in this folder a layout can be based on: protocol notes, saved layouts, earlier runs' layouts."""
    refs = self.references(text_limit=0)
    return any(refs[k] for k in ("protocols", "layouts", "run_layouts"))

  def references(self, text_limit: int = 8000) -> dict[str, Any]:
    """Protocols, saved layouts and the layouts earlier runs used, from this workspace only."""
    out: dict[str, Any] = {"workspace": str(self.root), "protocols": [], "layouts": [], "run_layouts": []}
    if not self.dir.is_dir():
      return out
    if self.protocols_dir.is_dir():
      for f in sorted(self.protocols_dir.rglob("*")):
        if f.is_file():
          text = f.read_text(errors="replace")
          out["protocols"].append({"name": str(f.relative_to(self.protocols_dir)), "path": str(f),
                                   "text": text[:text_limit], "truncated": len(text) > text_limit})
    if self.layouts_dir.is_dir():
      for f in sorted(self.layouts_dir.glob("*.yaml")):
        try:
          layout = yaml.safe_load(f.read_text())
        except yaml.YAMLError as exc:
          layout = {"unreadable": str(exc)}
        out["layouts"].append({"name": f.stem, "path": str(f), "layout": layout})
    seen: dict[str, dict[str, Any]] = {}

    def used(layout: dict[str, Any], saved_as: str | None, device: str | None, run: str) -> None:
      digest = hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()[:12]
      ref = seen.setdefault(digest, {"digest": digest, "device": device, "layout": layout, "runs": [],
                                     "saved_as": saved_as})
      if run not in ref["runs"]:
        ref["runs"].append(run)

    if self.runs_dir.is_dir():
      for log in sorted(self.runs_dir.glob("*.jsonl")):
        device, waiting = None, None
        for line in log.read_text().splitlines():
          try:
            entry = json.loads(line)
          except json.JSONDecodeError:
            continue
          if entry.get("type") == "run":
            device = entry.get("device")
          elif entry.get("op") == "load_layout" and entry.get("status") == "ok":
            layout, effective = (entry.get("basic") or {}).get("layout"), entry.get("effective") or {}
            waiting = None
            if not isinstance(layout, dict):
              continue
            if effective.get("needs_person"):  # a reference only once the person confirmed it
              waiting = (layout, effective.get("saved_as"))
            else:
              used(layout, effective.get("saved_as"), device, log.stem)
          elif entry.get("type") == "gate" and entry.get("state") == "passed" and waiting is not None \
              and entry.get("layout") == waiting[1]:
            used(*waiting, device, log.stem)
            waiting = None
    out["run_layouts"] = list(seen.values())
    return out
