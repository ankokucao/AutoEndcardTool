from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .constants import TARGETS, VIDEO_EXTENSIONS
from .media import append_endcard, classify_aspect, probe_media
from .models import BatchRequest, BatchSummary, MediaInfo


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class ScannedVideo:
    path: Path
    info: MediaInfo


def collect_video_files(input_root: Path, output_root: Path) -> list[Path]:
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    files: list[Path] = []
    for path in input_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        resolved = path.resolve()
        if resolved == output_root or output_root in resolved.parents:
            continue
        files.append(resolved)
    return sorted(files, key=lambda item: str(item).lower())


def output_path_for(source: Path, input_root: Path, output_root: Path, suffix: str) -> Path:
    relative = source.relative_to(input_root)
    return output_root / relative.parent / f"{source.stem}{suffix}.mp4"


class BatchProcessor:
    def __init__(self, ffmpeg: str, ffprobe: str) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def process(
        self,
        request: BatchRequest,
        stop_event: Event,
        on_log: LogCallback,
        on_progress: ProgressCallback,
    ) -> BatchSummary:
        input_root = request.input_dir.resolve()
        output_root = request.output_dir.resolve()
        endcard_path = request.endcard.resolve()
        files = [
            path
            for path in collect_video_files(input_root, output_root)
            if path != endcard_path
        ]
        summary = BatchSummary(total=len(files))
        on_progress(0, max(len(files), 1))

        if not files:
            on_log("没有找到可处理的视频文件。")
            return summary

        target_aspect = request.target_aspect
        if target_aspect not in TARGETS:
            on_log(f"扫描终止：不支持的目标比例 {target_aspect}。")
            return summary
        if endcard_path.suffix.lower() not in VIDEO_EXTENSIONS:
            on_log("扫描终止：片尾必须是支持的视频文件，不再接受图片片尾。")
            return summary

        on_log(f"开始扫描 {len(files)} 个原视频，本次只生成 {target_aspect}。")
        try:
            endcard_info = probe_media(endcard_path, self.ffprobe)
        except Exception as exc:
            on_log(f"扫描终止：无法读取片尾文件：{exc}")
            return summary

        endcard_aspect = classify_aspect(endcard_info.width, endcard_info.height)
        if endcard_aspect != target_aspect:
            detected = endcard_aspect or "不支持的比例"
            on_log(
                f"扫描终止：选择的是 {target_aspect}，但片尾为 {detected} "
                f"({endcard_info.width}x{endcard_info.height})。"
            )
            return summary
        on_log(
            f"片尾校验通过：{target_aspect}，"
            f"尺寸 {endcard_info.width}x{endcard_info.height}。"
        )

        matching: list[ScannedVideo] = []
        scan_skipped = 0
        scan_failed = 0
        for index, source in enumerate(files, start=1):
            if stop_event.is_set():
                summary.stopped = True
                on_log("扫描已停止，尚未开始编码。")
                return summary

            relative = source.relative_to(input_root)
            try:
                info = probe_media(source, self.ffprobe)
                source_aspect = classify_aspect(info.width, info.height)
                if source_aspect != target_aspect:
                    summary.skipped += 1
                    scan_skipped += 1
                    detected = source_aspect or "不支持的比例"
                    on_log(
                        f"扫描跳过：{relative} 为 {detected} "
                        f"({info.width}x{info.height})，与 {target_aspect} 片尾不一致。"
                    )
                    continue
                matching.append(ScannedVideo(source, info))
            except Exception as exc:
                summary.failed += 1
                scan_failed += 1
                on_log(f"扫描失败：{relative}：{exc}")
            finally:
                on_progress(index, len(files))

        on_log(
            f"扫描完成：符合 {target_aspect} 的视频 {len(matching)} 个，"
            f"比例不一致 {scan_skipped} 个，读取失败 {scan_failed} 个。"
        )
        if not matching:
            on_log("没有比例一致的视频，未生成任何文件。")
            return summary

        options = request.options
        on_log(
            f"裁剪设置：开头 {options.cut_head_seconds:.2f}s，"
            f"结尾 {options.cut_tail_seconds:.2f}s"
        )
        on_progress(0, len(matching))

        for index, candidate in enumerate(matching, start=1):
            if stop_event.is_set():
                summary.stopped = True
                on_log("已停止。")
                break

            source = candidate.path
            relative = source.relative_to(input_root)
            on_log(f"({index}/{len(matching)}) 处理：{relative}")
            try:
                info = candidate.info
                remaining = info.duration - options.cut_head_seconds - options.cut_tail_seconds
                if remaining <= 0.05:
                    summary.skipped += 1
                    on_log(f"  跳过：裁剪后仅剩 {remaining:.2f}s。")
                    continue

                output_path = output_path_for(
                    source,
                    input_root,
                    output_root,
                    options.suffix,
                )
                on_log(
                    f"  比例 {target_aspect}，保留 {remaining:.2f}s，"
                    f"片尾：{endcard_path.name}"
                )
                outcome = append_endcard(
                    self.ffmpeg,
                    self.ffprobe,
                    source,
                    endcard_path,
                    output_path,
                    target_aspect,
                    options,
                    source_info=info,
                )
                if outcome.status == "succeeded":
                    summary.succeeded += 1
                    on_log(f"  完成：{output_path}")
                elif outcome.status == "skipped":
                    summary.skipped += 1
                    on_log(f"  跳过：{outcome.message}")
                else:
                    summary.failed += 1
                    on_log(f"  失败：{outcome.message}")
            except Exception as exc:
                summary.failed += 1
                on_log(f"  失败：{exc}")
            finally:
                on_progress(index, len(matching))

        on_log(
            f"处理结束：成功 {summary.succeeded}，"
            f"跳过 {summary.skipped}，失败 {summary.failed}"
        )
        return summary
