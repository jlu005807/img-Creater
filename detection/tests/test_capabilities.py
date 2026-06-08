import importlib
import io
import unittest

from detection import capabilities, detector
from detection.config import load_config

# The analyzer names the detector dynamically imports (detector.py stages 1+2).
_ANALYZER_NAMES = ("watermark", "metadata", "frequency", "noise", "jpeg", "color")
# The subset fused by the scoring engine (must match the config weight keys).
_FUSION_NAMES = ("frequency", "noise", "jpeg", "color")


class CapabilitiesTests(unittest.TestCase):
    def test_capability_report_shape(self):
        rep = capabilities.capability_report()
        self.assertIn("available", rep)
        self.assertIn("missing_required", rep)
        self.assertIn("missing_optional", rep)
        self.assertIn("analyzers", rep)
        self.assertIsInstance(rep["analyzers"], dict)

    def test_health_matches_report(self):
        self.assertEqual(detector.detector_health(), capabilities.capability_report())

    def test_detect_when_unavailable_does_not_raise(self):
        # Regardless of installed deps, detect_image must return a well-formed
        # dict and never raise. When required deps are missing it reports
        # unavailable; otherwise it returns a verdict.
        result = detector.detect_image(b"not really an image", filename="x.png")
        self.assertIn("verdict", result)
        self.assertIn(result["verdict"], {"ai", "suspicious", "real", "unavailable"})
        self.assertIn("elapsed_ms", result)

    def test_config_defaults_load(self):
        cfg = load_config()
        self.assertIn("weights", cfg)
        self.assertIn("thresholds", cfg)
        self.assertAlmostEqual(sum(cfg["weights"].values()), 1.0, places=3)

    def test_config_bad_path_falls_back(self):
        cfg = load_config("/nonexistent/path/config.json")
        self.assertIn("weights", cfg)

    def test_iterated_analyzers_resolve(self):
        # Every analyzer name the detector imports must resolve to a module
        # exposing a callable analyze(). Guards the name<->filename mismatch
        # class: the detector asks for "jpeg", so the file must be jpeg.py.
        for name in _ANALYZER_NAMES:
            mod = importlib.import_module(f"detection.analyzers.{name}")
            self.assertTrue(
                callable(getattr(mod, "analyze", None)),
                f"analyzer '{name}' must expose a callable analyze()",
            )

    def test_names_agree_across_config_and_capabilities(self):
        # Fusion weight keys must equal the analyzers the detector fuses, and
        # every iterated analyzer must appear in the capability report — so a
        # rename can't silently drop an analyzer out of scoring.
        cfg = load_config()
        self.assertEqual(set(cfg["weights"]), set(_FUSION_NAMES))
        rep = capabilities.capability_report()
        for name in _ANALYZER_NAMES:
            self.assertIn(name, rep["analyzers"])

    @unittest.skipUnless(capabilities.is_available(), "core deps (numpy/Pillow) absent")
    def test_detect_on_valid_image_respects_contract(self):
        import numpy as np
        from PIL import Image

        # Deterministic gradient so the signal analyzers actually produce scores.
        arr = (np.indices((300, 300)).sum(axis=0) % 256).astype("uint8")
        buf = io.BytesIO()
        Image.fromarray(arr).convert("RGB").save(buf, format="PNG")

        result = detector.detect_image(buf.getvalue(), filename="x.png")
        self.assertIn(result["verdict"], {"ai", "suspicious", "real"})
        score = result["score"]
        self.assertTrue(score is None or 0.0 <= score <= 1.0)
        for name, stage in result["stages"].items():
            self.assertIsInstance(stage, dict, f"stage '{name}' must be a dict")
            s = stage.get("score")
            self.assertTrue(
                s is None or 0.0 <= s <= 1.0, f"stage '{name}' score out of range: {s}"
            )


if __name__ == "__main__":
    unittest.main()
