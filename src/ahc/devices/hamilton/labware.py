"""Hamilton labware AHC defines itself: where PyLabRobot has no definition, or one that disagrees with Hamilton's."""

from __future__ import annotations

from typing import Any, Callable

from pylabrobot.resources import Coordinate, ResourceHolder
from pylabrobot.resources.carrier import TroughCarrier, create_homogeneous_resources


def RGT_CAR_5R60_A00(name: str) -> TroughCarrier:
  """Hamilton's reagent carrier for 5 containers of 60 mL (53646-01), one track wide. It carries
  `hamilton_1_trough_60mL_Vb`.

  The five sites are 96 mm apart, as in Hamilton's own definition of the carrier. PyLabRobot's
  `Trough_CAR_5R60_A00` has four sites, the fourth 7.5 mm behind Hamilton's, so AHC does not list that one.
  X and Y put PyLabRobot's trough model (19 x 90 mm) on the centre of Hamilton's site (20 x 89.9 mm at X 1.25,
  Y 6.5 + 96 n). Z is PyLabRobot's measured 63.5 mm, 0.3 mm above Hamilton's 63.2.
  """
  return TroughCarrier(
    name=name, size_x=22.5, size_y=497.0, size_z=93.0,
    sites=create_homogeneous_resources(
      klass=ResourceHolder, locations=[Coordinate(1.75, 6.45 + 96.0 * n, 63.5) for n in range(5)],
      resource_size_x=19.0, resource_size_y=90.0, name_prefix=name),
    model="RGT_CAR_5R60_A00")


# Carrier types AHC builds itself, by the name a layout gives; every other listed type is PyLabRobot's.
CARRIERS: dict[str, Callable[[str], Any]] = {"RGT_CAR_5R60_A00": RGT_CAR_5R60_A00}
