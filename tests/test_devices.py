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
