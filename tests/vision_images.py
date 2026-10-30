"""Synthetic deck pictures for the vision tests: an OT-2 deck seen from above, drawn with OpenCV.

The slot boxes follow the real USB camera's geometry (front row at the bottom, slots 7-9 cut at the top edge),
scaled to a 1280 x 720 frame: a slot's footprint is about 317 x 215 px, 2.5 px per mm.
"""

from __future__ import annotations

import cv2
import numpy as np

W, H = 1280, 720
SLOT_W, SLOT_H = 317, 215


def slot_box(n: int) -> list[float]:
  """Slot n's footprint in the reference picture; row 0 (slots 1-3) is at the bottom."""
  col, row = (n - 1) % 3, (n - 1) // 3
  x0 = 141 + 329 * col
  y0 = 371 - 224 * row
  return [float(x0), float(y0), float(x0 + SLOT_W), float(y0 + SLOT_H)]


def deck(seed: int = 1, dx: float = 0, dy: float = 0, angle: float = 0.0, blur: int = 0, noise: bool = True) -> np.ndarray:
  """A deck picture: texture the features can hold on to, a labelled plate in every slot. dx, dy and angle move the
  camera; blur makes the frame out of focus."""
  rng = np.random.default_rng(seed)
  image = np.full((H, W, 3), 205, np.uint8)
  for _ in range(260):  # clips, screws and scratches
    x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
    color = tuple(int(c) for c in rng.integers(60, 160, 3))
    if rng.random() < 0.5:
      cv2.circle(image, (x, y), int(rng.integers(3, 9)), color, -1)
    else:
      cv2.line(image, (x, y), (x + int(rng.integers(-40, 40)), y + int(rng.integers(-40, 40))), color, 2)
  for n in range(1, 10):
    x0, y0, x1, y1 = (int(v) for v in slot_box(n))
    cv2.rectangle(image, (x0, y0), (x1, y1), (90, 90, 90), 3)
    cv2.putText(image, str(n), (x0 + 120, y0 + 140), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (70, 70, 140), 6)
    pitch = SLOT_W / 14.2
    for r in range(8):
      for c in range(12):
        cv2.circle(image, (int(x0 + 16 + (c + 0.5) * pitch * 0.98), int(y0 + 14 + (r + 0.5) * pitch * 0.98)),
                   int(pitch * 0.32), (120 + 12 * r, 90, 160 - 9 * c), 2)
  if noise:
    image = cv2.add(image, rng.integers(0, 6, image.shape, dtype=np.uint8))
  matrix = cv2.getRotationMatrix2D((W / 2, H / 2), angle, 1.0)
  matrix[:, 2] += (dx, dy)
  image = cv2.warpAffine(image, matrix, (W, H), borderMode=cv2.BORDER_REPLICATE)
  if blur:
    image = cv2.GaussianBlur(image, (blur | 1, blur | 1), 0)
  return image


def jpeg(image: np.ndarray, width: int | None = None) -> bytes:
  if width is not None and width != image.shape[1]:
    image = cv2.resize(image, (width, int(image.shape[0] * width / image.shape[1])), interpolation=cv2.INTER_AREA)
  ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
  assert ok
  return data.tobytes()


def other_scene(seed: int = 7) -> np.ndarray:
  """A picture of something else (the camera was turned away): the same sensor, no deck in it."""
  rng = np.random.default_rng(seed)
  image = np.full((H, W, 3), 120, np.uint8)
  for _ in range(300):
    x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
    color = tuple(int(c) for c in rng.integers(0, 255, 3))
    cv2.rectangle(image, (x, y), (x + int(rng.integers(5, 60)), y + int(rng.integers(5, 40))), color, -1)
  return image
