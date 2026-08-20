import unittest

from auto_endcard.media import _concat_filter, classify_aspect
from auto_endcard.models import MediaInfo


class AspectClassificationTests(unittest.TestCase):
    def test_supported_aspects(self) -> None:
        self.assertEqual(classify_aspect(1920, 1080), "16:9")
        self.assertEqual(classify_aspect(1080, 1080), "1:1")
        self.assertEqual(classify_aspect(1080, 1920), "9:16")

    def test_unsupported_aspect_is_rejected(self) -> None:
        self.assertIsNone(classify_aspect(1000, 700))
        self.assertIsNone(classify_aspect(0, 1080))

    def test_concat_filter_stretches_both_video_streams(self) -> None:
        media = MediaInfo(1280, 720, 5.0, False)
        filter_graph = _concat_filter("16:9", media, 0.0, 5.0, 5.0, media, 30)

        self.assertEqual(filter_graph.count("scale=1920:1080"), 2)
        self.assertNotIn("force_original_aspect_ratio", filter_graph)
        self.assertNotIn("pad=", filter_graph)


if __name__ == "__main__":
    unittest.main()
