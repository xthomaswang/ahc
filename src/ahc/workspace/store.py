"""The workspace: one task folder, with everything AHC keeps for that task under `<folder>/.ahc/`.

    .ahc/config.yaml   devices, task limits, confirmation, base simulator versions used
    .ahc/layouts/      layouts the agent wrote (saved by load_layout, or once the person confirms them)
    .ahc/protocols/    protocols the agent consults when writing a layout
    .ahc/runs/         run logs
    .ahc/sim/          this task's simulator settings and state
    .ahc/state/deck.json  the digital twin's deck after the last call that changed it (volumes, tip spots, tips
                          on the pipettes, what is unknown): find_layout_references offers it as the last deck
    .ahc/tips-left.json  the tips a real robot's run holds or may hold (from each pick-up, a stop, an unknown
                         outcome, a tips check the person failed, the server ending): the robot forgets them once
                         that run stops

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
SAVED_FOR = re.compile(r"# Layout for (\S+), saved by AHC")


def resolve_workspace(arg: str | os.PathLike | None = None) -> Path:
  """--workspace, else AHC_WORKSPACE, else the folder the server was started in."""
  chosen = arg or os.environ.get("AHC_WORKSPACE") or os.getcwd()
  return Path(chosen).expanduser().resolve()


def ahc_home() -> Path:
  """AHC's own data folder, shared by every task (the OT-2 simulator base): AHC_HOME, else ~/.ahc."""
  return Path(os.environ.get("AHC_HOME") or "~/.ahc").expanduser()


def write_atomic(path: Path, text: str) -> None:
  """Write through a temporary file and a rename: a reader (another server, ahc-sim) never sees half a file."""
  tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
  try:
    tmp.write_text(text)
    os.replace(tmp, path)
  except BaseException:
    tmp.unlink(missing_ok=True)  # a failed write leaves the old file as it was, and no temporary file
    raise


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

  def require_task_folder(self) -> None:
    """Refuse to write where this folder's .ahc is AHC's own data folder: in the home folder, ~/.ahc
    holds what every task shares, and a Claude Code session started there is no task."""
    if self.dir.resolve() == ahc_home().resolve():
      raise LabError("not_a_task_folder", f"{self.dir} is AHC's own data folder (AHC_HOME), not a task's.",
                     "Open the agent in a folder for this task (not the home folder), or pass --workspace / "
                     "AHC_WORKSPACE.")

  def ensure(self) -> None:
    if not self.root.is_dir():
      raise LabError("no_workspace", f"workspace folder {self.root} does not exist.",
                     "Start the server in the task folder, or pass --workspace / AHC_WORKSPACE.")
    self.require_task_folder()
    for sub in SUBDIRS:
      (self.dir / sub).mkdir(parents=True, exist_ok=True)

  # -- config file ----------------------------------------------------------------------------

  def read_config_text(self) -> str | None:
    return self.config_path.read_text() if self.config_path.is_file() else None

  def write_config(self, data: dict[str, Any], header: str) -> None:
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    self.write_config_text(header.rstrip() + "\n" + body)

  def write_config_text(self, text: str) -> None:
    """The config file as given (the template too), never half written."""
    self.ensure()
    write_atomic(self.config_path, text)

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
    write_atomic(path, header + yaml.safe_dump(layout, sort_keys=False, allow_unicode=True))
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

  # -- tips a real device was dropped with ------------------------------------------------------

  @property
  def tips_left_path(self) -> Path:
    return self.dir / "tips-left.json"

  def mark_tips_left(self, note: dict[str, Any]) -> None:
    self.dir.mkdir(parents=True, exist_ok=True)
    write_atomic(self.tips_left_path, json.dumps(note, indent=2) + "\n")

  def tips_left(self) -> dict[str, Any] | None:
    """The note, or None when there is none. A note that exists but cannot be read is {"unreadable": why}: tips may
    be on all the same."""
    try:
      note = json.loads(self.tips_left_path.read_text())
    except FileNotFoundError:
      return None
    except (OSError, ValueError) as exc:
      return {"unreadable": f"{type(exc).__name__}: {exc}"}
    return note if isinstance(note, dict) else {"unreadable": f"it holds a JSON {type(note).__name__}, not a note"}

  def clear_tips_left(self) -> None:
    self.tips_left_path.unlink(missing_ok=True)

  # -- the deck the last run left ---------------------------------------------------------------

  @property
  def deck_path(self) -> Path:
    return self.dir / "state" / "deck.json"

  def write_deck(self, deck: dict[str, Any]) -> bool:
    """The twin's deck state, never half written. Not rewritten when only its time changed. False: nothing written."""
    old = self.read_deck()
    if old is not None and {k: v for k, v in old.items() if k != "at"} == {k: v for k, v in deck.items() if k != "at"}:
      return False
    self.require_task_folder()
    self.deck_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(self.deck_path, json.dumps(deck, indent=2) + "\n")
    return True

  def read_deck(self) -> dict[str, Any] | None:
    """The saved deck state, or None when there is none or it cannot be read."""
    try:
      deck = json.loads(self.deck_path.read_text())
    except (OSError, ValueError):
      return None
    return deck if isinstance(deck, dict) and isinstance(deck.get("layout"), dict) else None

  # -- the folder's record ------------------------------------------------------------------------

  def ran_experiment(self) -> bool:
    """This folder has run an experiment: a run log in .ahc/runs/ holds a load_layout that went through
    (any device, any backend). A folder that never did needs the person's go-ahead even in simulation."""
    if not self.runs_dir.is_dir():
      return False
    for log in sorted(self.runs_dir.glob("*.jsonl")):
      try:
        with log.open() as f:
          for line in f:
            if '"load_layout"' not in line:  # most lines: no need to parse them
              continue
            try:
              entry = json.loads(line)
            except json.JSONDecodeError:
              continue
            if isinstance(entry, dict) and entry.get("op") == "load_layout" and entry.get("status") == "ok":
              return True
      except OSError:
        continue
    return False

  # -- references for writing a layout ------------------------------------------------------------

  def has_reference(self, model: str | None = None) -> bool:
    """A layout for `model` here that a new one can be matched against (reference_layouts)."""
    return bool(self.reference_layouts(model))

  def reference_layouts(self, model: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    """The layouts a new layout for `model` is matched against, each with the name to show for it: every one saved
    here for it, and every one an earlier run here used once its deck check passed. A protocol is none: nothing in
    it can be matched. Layouts saved before the person had to confirm them count as well; unreadable ones do not."""
    refs = self.references(text_limit=0, model=model, protocols=False)
    found = [(f"saved layout {r['name']!r}", r["layout"]) for r in refs["layouts"]]
    found += [(f"layout {r['saved_as']!r} of run {r['runs'][-1]}" if r.get("saved_as") else
               f"the layout of run {r['runs'][-1]}", r["layout"]) for r in refs["run_layouts"]]
    return [(name, layout) for name, layout in found if isinstance(layout, dict) and "unreadable" not in layout]

  def references(self, text_limit: int = 8000, model: str | None = None, protocols: bool = True) -> dict[str, Any]:
    """Protocols, saved layouts and the layouts earlier runs used, from this workspace only.

    A protocol counts unless it is hidden or empty (a Finder .DS_Store is no reference). With a model,
    only layouts saved for it and run layouts used on it count. A run's layout counts only once its
    deck check passed: a layout the person rejected, or never checked, is no reference.
    """
    out: dict[str, Any] = {"workspace": str(self.root), "protocols": [], "layouts": [], "run_layouts": []}
    if not self.dir.is_dir():
      return out
    if protocols and self.protocols_dir.is_dir():
      for f in sorted(self.protocols_dir.rglob("*")):
        relative = f.relative_to(self.protocols_dir)
        if not f.is_file() or any(part.startswith(".") for part in relative.parts):
          continue
        text = f.read_text(errors="replace")
        if not text.strip():
          continue
        out["protocols"].append({"name": str(relative), "path": str(f),
                                 "text": text[:text_limit], "truncated": len(text) > text_limit})
    if self.layouts_dir.is_dir():
      for f in sorted(self.layouts_dir.glob("*.yaml")):
        text = f.read_text(errors="replace")
        saved_for = SAVED_FOR.search(text)
        if model is not None and (saved_for is None or saved_for.group(1) != model):
          continue
        try:
          layout = yaml.safe_load(text)
        except yaml.YAMLError as exc:
          layout = {"unreadable": str(exc)}
        out["layouts"].append({"name": f.stem, "path": str(f), "model": saved_for.group(1) if saved_for else None,
                               "layout": layout})
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
            waiting = (layout, effective.get("saved_as")) if isinstance(layout, dict) else None
          elif entry.get("type") == "gate" and entry.get("state") == "passed" and waiting is not None \
              and entry.get("layout") == waiting[1]:
            if model is None or device == model:
              used(*waiting, device, log.stem)
            waiting = None
    out["run_layouts"] = list(seen.values())
    return out
