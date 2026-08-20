import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from auto_endcard.models import BatchRequest, EncodeOutcome, EncodingOptions, MediaInfo
from auto_endcard.processor import BatchProcessor, collect_video_files, output_path_for


class FileCollectionTests(unittest.TestCase):
    def test_collects_recursively_and_excludes_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            output_dir = input_dir / "output"
            nested = input_dir / "nested"
            nested.mkdir(parents=True)
            output_dir.mkdir()
            (input_dir / "a.mp4").touch()
            (nested / "b.MOV").touch()
            (nested / "ignore.txt").touch()
            (output_dir / "generated.mp4").touch()

            files = collect_video_files(input_dir, output_dir)
            self.assertEqual([path.name for path in files], ["a.mp4", "b.MOV"])

    def test_output_path_preserves_relative_directory(self) -> None:
        input_dir = Path("C:/input")
        output_dir = Path("C:/output")
        source = input_dir / "nested" / "clip.mov"
        self.assertEqual(
            output_path_for(source, input_dir, output_dir, "_endcard"),
            output_dir / "nested" / "clip_endcard.mp4",
        )


class BatchScanningTests(unittest.TestCase):
    def _request(self, root: Path, target_aspect: str = "16:9") -> BatchRequest:
        return BatchRequest(
            input_dir=root / "input",
            output_dir=root / "output",
            target_aspect=target_aspect,
            endcard=root / "endcard.mp4",
            options=EncodingOptions(),
        )

    @patch("auto_endcard.processor.append_endcard")
    @patch("auto_endcard.processor.probe_media")
    def test_rejects_image_endcard_before_probe(
        self,
        probe_media_mock,
        append_endcard_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "wide.mp4").touch()
            image_endcard = root / "endcard.png"
            image_endcard.touch()
            request = BatchRequest(
                input_dir=input_dir,
                output_dir=root / "output",
                target_aspect="16:9",
                endcard=image_endcard,
                options=EncodingOptions(),
            )
            logs: list[str] = []

            BatchProcessor("ffmpeg", "ffprobe").process(
                request,
                Event(),
                logs.append,
                lambda _current, _total: None,
            )

            probe_media_mock.assert_not_called()
            append_endcard_mock.assert_not_called()
            self.assertTrue(any("不再接受图片片尾" in message for message in logs))

    @patch("auto_endcard.processor.append_endcard")
    @patch("auto_endcard.processor.probe_media")
    def test_scans_all_sources_before_processing_and_filters_ratio(
        self,
        probe_media_mock,
        append_endcard_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            input_dir.mkdir()
            wide = input_dir / "wide.mp4"
            square = input_dir / "square.mp4"
            endcard = root / "endcard.mp4"
            wide.touch()
            square.touch()
            endcard.touch()
            events: list[str] = []

            def probe(path: Path, _ffprobe: str) -> MediaInfo:
                events.append(f"probe:{path.name}")
                if path.name == "square.mp4":
                    return MediaInfo(1080, 1080, 5.0, True)
                return MediaInfo(1920, 1080, 5.0, True)

            def append(*args, **kwargs) -> EncodeOutcome:
                events.append(f"append:{args[2].name}")
                return EncodeOutcome("succeeded", "完成")

            probe_media_mock.side_effect = probe
            append_endcard_mock.side_effect = append
            summary = BatchProcessor("ffmpeg", "ffprobe").process(
                self._request(root),
                Event(),
                lambda _message: None,
                lambda _current, _total: None,
            )

            first_append = next(index for index, event in enumerate(events) if event.startswith("append:"))
            last_probe = max(index for index, event in enumerate(events) if event.startswith("probe:"))
            self.assertGreater(first_append, last_probe)
            self.assertEqual(append_endcard_mock.call_count, 1)
            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(summary.skipped, 1)

    @patch("auto_endcard.processor.append_endcard")
    @patch("auto_endcard.processor.probe_media")
    def test_rejects_endcard_with_wrong_aspect(
        self,
        probe_media_mock,
        append_endcard_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "wide.mp4").touch()
            (root / "endcard.mp4").touch()
            probe_media_mock.return_value = MediaInfo(1080, 1080, 5.0, False)
            logs: list[str] = []

            summary = BatchProcessor("ffmpeg", "ffprobe").process(
                self._request(root),
                Event(),
                logs.append,
                lambda _current, _total: None,
            )

            append_endcard_mock.assert_not_called()
            self.assertEqual(summary.succeeded, 0)
            self.assertTrue(any("扫描终止" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
