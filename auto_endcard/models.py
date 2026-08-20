from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .constants import TARGETS


@dataclass(frozen=True, slots=True)
class MediaInfo:
    width: int
    height: int
    duration: float
    has_audio: bool
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class EncodingOptions:
    cut_head_seconds: float = 0.0
    cut_tail_seconds: float = 0.0
    fps: int = 30
    crf: int = 18
    preset: str = "medium"
    overwrite: bool = True
    suffix: str = "_endcard"


@dataclass(frozen=True, slots=True)
class BatchRequest:
    input_dir: Path
    output_dir: Path
    target_aspect: str
    endcard: Path
    options: EncodingOptions


@dataclass(frozen=True, slots=True)
class EncodeOutcome:
    status: str
    message: str


@dataclass(slots=True)
class BatchSummary:
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False


@dataclass(slots=True)
class AppConfig:
    input_dir: str = ""
    output_dir: str = ""
    target_aspect: str = "16:9"
    outro_16x9: str = ""
    outro_1x1: str = ""
    outro_9x16: str = ""
    cut_head_seconds: float = 0.0
    cut_tail_seconds: float = 0.0
    fps: int = 30
    crf: int = 18
    preset: str = "medium"
    overwrite: bool = True
    suffix: str = "_endcard"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AppConfig":
        defaults = cls()

        def text(key: str) -> str:
            value = data.get(key, getattr(defaults, key))
            return str(value) if value is not None else ""

        def number(key: str, converter: type[int] | type[float]) -> int | float:
            try:
                return converter(data.get(key, getattr(defaults, key)))
            except (TypeError, ValueError):
                return getattr(defaults, key)

        overwrite = data.get("overwrite", defaults.overwrite)
        if not isinstance(overwrite, bool):
            overwrite = defaults.overwrite

        target_aspect = text("target_aspect")
        if target_aspect not in TARGETS:
            target_aspect = defaults.target_aspect

        return cls(
            input_dir=text("input_dir"),
            output_dir=text("output_dir"),
            target_aspect=target_aspect,
            outro_16x9=text("outro_16x9"),
            outro_1x1=text("outro_1x1"),
            outro_9x16=text("outro_9x16"),
            cut_head_seconds=float(number("cut_head_seconds", float)),
            cut_tail_seconds=float(number("cut_tail_seconds", float)),
            fps=int(number("fps", int)),
            crf=int(number("crf", int)),
            preset=text("preset") or defaults.preset,
            overwrite=overwrite,
            suffix=text("suffix"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)
