from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .constants import ASPECT_TOLERANCE, TARGETS
from .models import EncodeOutcome, EncodingOptions, MediaInfo


def _subprocess_flags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_rotation(video_stream: dict[str, Any]) -> int:
    candidates: list[Any] = [video_stream.get("tags", {}).get("rotate")]
    for item in video_stream.get("side_data_list", []):
        candidates.append(item.get("rotation"))
        display_matrix = str(item.get("displaymatrix", ""))
        match = re.search(r"rotation\s+of\s+(-?\d+(?:\.\d+)?)", display_matrix, re.I)
        if match:
            candidates.append(match.group(1))

    for value in candidates:
        if value is None:
            continue
        rotation = int(round(_safe_float(value))) % 360
        if rotation:
            return rotation
    return 0


def probe_media(path: Path, ffprobe: str) -> MediaInfo:
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_subprocess_flags(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")

    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        raise RuntimeError("未找到视频流")

    video = video_streams[0]
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _parse_rotation(video)
    if rotation in {90, 270}:
        width, height = height, width

    duration = _safe_float(payload.get("format", {}).get("duration"))
    if duration <= 0:
        duration = _safe_float(video.get("duration"))
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return MediaInfo(width, height, duration, has_audio, rotation)


def classify_aspect(width: int, height: int, tolerance: float = ASPECT_TOLERANCE) -> str | None:
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    name, difference = min(
        ((name, abs(ratio - target.ratio)) for name, target in TARGETS.items()),
        key=lambda item: item[1],
    )
    return name if difference <= tolerance else None


def _concat_filter(
    target_name: str,
    source: MediaInfo,
    trim_start: float,
    trim_end: float,
    trim_duration: float,
    endcard: MediaInfo,
    fps: int,
) -> str:
    target = TARGETS[target_name]
    width, height = target.width, target.height
    filters = [
        (
            f"[0:v:0]trim=start={trim_start:.3f}:end={trim_end:.3f},"
            "setpts=PTS-STARTPTS,"
            f"scale={width}:{height},"
            f"fps={fps},setsar=1,format=yuv420p[v0]"
        ),
        (
            f"[1:v:0]scale={width}:{height},"
            f"fps={fps},setsar=1,format=yuv420p[v1]"
        ),
    ]

    if source.has_audio:
        filters.append(
            f"[0:a:0]atrim=start={trim_start:.3f}:end={trim_end:.3f},"
            "asetpts=PTS-STARTPTS,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a0]"
        )
    else:
        filters.append(
            "anullsrc=channel_layout=stereo:sample_rate=48000:"
            f"d={trim_duration:.3f}[a0]"
        )

    if endcard.has_audio:
        filters.append(
            "[1:a:0]aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo,asetpts=PTS-STARTPTS[a1]"
        )
    else:
        filters.append(
            "anullsrc=channel_layout=stereo:sample_rate=48000:"
            f"d={endcard.duration:.3f}[a1]"
        )

    filters.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    return ";".join(filters)


def append_endcard(
    ffmpeg: str,
    ffprobe: str,
    input_path: Path,
    endcard_path: Path,
    output_path: Path,
    target_name: str,
    options: EncodingOptions,
    source_info: MediaInfo | None = None,
) -> EncodeOutcome:
    source = source_info or probe_media(input_path, ffprobe)
    trim_start = max(0.0, options.cut_head_seconds)
    trim_end = max(trim_start, source.duration - max(0.0, options.cut_tail_seconds))
    trim_duration = trim_end - trim_start
    if trim_duration <= 0.05:
        return EncodeOutcome("skipped", f"视频过短，裁剪后仅剩 {trim_duration:.2f}s")

    endcard = probe_media(endcard_path, ffprobe)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not options.overwrite:
        return EncodeOutcome("skipped", "已存在，跳过")

    command = [ffmpeg, "-y" if options.overwrite else "-n", "-hide_banner", "-loglevel", "error"]
    command.extend(["-i", str(input_path)])
    command.extend(["-i", str(endcard_path)])
    command.extend(
        [
            "-filter_complex",
            _concat_filter(
                target_name,
                source,
                trim_start,
                trim_end,
                trim_duration,
                endcard,
                options.fps,
            ),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            options.preset,
            "-crf",
            str(options.crf),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_subprocess_flags(),
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "ffmpeg failed"
        return EncodeOutcome("failed", message[-3000:])
    return EncodeOutcome("succeeded", "完成")
