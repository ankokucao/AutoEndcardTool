import json
import tempfile
import unittest
from pathlib import Path

from auto_endcard.config import ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def test_legacy_box_fields_are_dropped_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "input_dir": "input",
                        "output_dir": "output",
                        "process_mode": "box",
                        "preview_source": "preview.mp4",
                        "image_duration": 5,
                        "box_output_1x1": True,
                        "box_bg_1x1": "background.png",
                        "layouts": {"1:1": {"x_pct": 0.1}},
                    }
                ),
                encoding="utf-8",
            )

            store = ConfigStore(path)
            config = store.load()
            store.save(config)
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(saved["input_dir"], "input")
            self.assertEqual(saved["target_aspect"], "16:9")
            self.assertNotIn("process_mode", saved)
            self.assertNotIn("preview_source", saved)
            self.assertNotIn("image_duration", saved)
            self.assertFalse(any(key.startswith("box_") for key in saved))
            self.assertNotIn("layouts", saved)

    def test_invalid_target_aspect_falls_back_to_16x9(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"target_aspect": "4:3"}', encoding="utf-8")

            config = ConfigStore(path).load()

            self.assertEqual(config.target_aspect, "16:9")


if __name__ == "__main__":
    unittest.main()
