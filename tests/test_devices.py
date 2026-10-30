import pytest

from ahc.devices.spec import BASIC_LIMITS, load_device, parse_device

MODELS = ["hamilton.starlet", "opentrons.ot2"]


@pytest.mark.parametrize("model", MODELS)
def test_every_basic_limit_names_its_source(model):
  spec = load_device(model)
  for comp in spec.components.values():
    if "liquid_handling" in comp.capabilities:
      for key in BASIC_LIMITS:
        assert comp.limits[key].source, f"{model}.{comp.name}.{key} has no source"


@pytest.mark.parametrize("model", MODELS)
def test_every_liquid_height_has_a_default_inside_its_limits(model):
  # An omitted height is the description's default and is recorded as such, never an unrecorded device choice.
  spec = load_device(model)
  for comp in spec.components.values():
    if "liquid_handling" in comp.capabilities:
      assert comp.limits["liquid_height_mm"].covers_default(), f"{model}.{comp.name}"


def test_the_ot2_stays_one_mm_above_the_well_floor_by_default():
  spec = load_device("opentrons.ot2")
  assert {c.limits["liquid_height_mm"].default for c in spec.components.values()} == {1.0}


def _write(tmp_path, components: str):
  path = tmp_path / "bad.md"
  path.write_text(f"---\nmodel: x.y\nbrand: x\ntitle: t\ncomponents:\n{components}---\n")
  return path


def test_a_capability_without_limits_is_rejected(tmp_path):
  path = _write(tmp_path, "  pip:\n    capabilities: [liquid_handling]\n    channels: 1\n")
  with pytest.raises(ValueError, match="needs limits"):
    parse_device(path)


def test_specialized_parameters_must_belong_to_an_operation(tmp_path):
  limits = "".join(f"      {k}: {{min: 0, max: 1, source: s}}\n" for k in BASIC_LIMITS)
  path = _write(tmp_path, "  pip:\n    capabilities: [liquid_handling]\n    channels: 1\n"
                          f"    limits:\n{limits}    specialized:\n      fly: {{}}\n")
  with pytest.raises(ValueError, match="not a liquid-handling op"):
    parse_device(path)


def test_one_version_everywhere():
  import json
  import tomllib
  from pathlib import Path

  import ahc

  root = Path(__file__).resolve().parents[1]
  version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
  assert ahc.__version__ == version  # what the MCP server reports
  assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"] == version
