from __future__ import annotations

from dataclasses import dataclass


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"})

FPS_CHOICES = (24, 25, 30, 60)
CRF_CHOICES = (16, 18, 20, 22, 24)
PRESET_CHOICES = ("fast", "medium", "slow")
ASPECT_TOLERANCE = 0.06
CONFIG_FILENAME = "auto_endcard_config.json"


@dataclass(frozen=True, slots=True)
class TargetSpec:
    name: str
    width: int
    height: int

    @property
    def ratio(self) -> float:
        return self.width / self.height


TARGETS: dict[str, TargetSpec] = {
    "16:9": TargetSpec("16:9", 1920, 1080),
    "1:1": TargetSpec("1:1", 1080, 1080),
    "9:16": TargetSpec("9:16", 1080, 1920),
}
