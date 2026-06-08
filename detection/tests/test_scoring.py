import unittest

from detection.scoring import fuse_scores, high_confidence_result


WEIGHTS = {"frequency": 0.3, "noise": 0.25, "jpeg": 0.25, "color": 0.2}
THRESHOLDS = {"ai": 0.6, "suspicious": 0.3}


class ScoringTests(unittest.TestCase):
    def test_all_high_scores_yield_ai(self):
        res = fuse_scores(
            {"frequency": 0.9, "noise": 0.9, "jpeg": 0.9, "color": 0.9}, WEIGHTS, THRESHOLDS
        )
        self.assertEqual(res["verdict"], "ai")
        self.assertGreaterEqual(res["score"], 0.6)

    def test_all_low_scores_yield_real(self):
        res = fuse_scores(
            {"frequency": 0.1, "noise": 0.1, "jpeg": 0.1, "color": 0.1}, WEIGHTS, THRESHOLDS
        )
        self.assertEqual(res["verdict"], "real")

    def test_mid_scores_yield_suspicious(self):
        res = fuse_scores(
            {"frequency": 0.45, "noise": 0.45, "jpeg": 0.45, "color": 0.45}, WEIGHTS, THRESHOLDS
        )
        self.assertEqual(res["verdict"], "suspicious")

    def test_missing_modules_are_renormalized(self):
        # Only frequency contributes; its 0.9 should drive the whole score,
        # not be diluted by the absent modules' weights.
        res = fuse_scores(
            {"frequency": 0.9, "noise": None, "jpeg": None, "color": None}, WEIGHTS, THRESHOLDS
        )
        self.assertAlmostEqual(res["score"], 0.9, places=3)
        self.assertEqual(res["verdict"], "ai")
        self.assertEqual(set(res["used_weights"]), {"frequency"})

    def test_no_signal_is_suspicious_not_real(self):
        res = fuse_scores(
            {"frequency": None, "noise": None, "jpeg": None, "color": None}, WEIGHTS, THRESHOLDS
        )
        self.assertEqual(res["verdict"], "suspicious")

    def test_high_confidence_helper(self):
        res = high_confidence_result(1.0, ["命中水印"])
        self.assertEqual(res["verdict"], "ai")
        self.assertEqual(res["score"], 1.0)
        self.assertIn("命中水印", res["evidence"])


if __name__ == "__main__":
    unittest.main()
