from pathlib import Path
from unittest import TestCase, main


REGION_EDITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "RegionEditor" / "index.vue"


class RegionEditorSourceTests(TestCase):
    def test_marker_color_palette_is_persisted_and_export_preserves_region_colors(self):
        source = REGION_EDITOR.read_text(encoding="utf-8")

        self.assertIn("const markColors = [", source)
        self.assertIn("markerColor", source)
        self.assertIn("draft.markerColor", source)
        self.assertIn("markerColorRgba", source)
        self.assertIn("maskContext.strokeStyle = markerColorRgba(1)", source)
        self.assertIn("maskContext.fillStyle = markerColorRgba(1)", source)
        self.assertIn("markContext.globalAlpha = OVERLAY_ALPHA", source)
        self.assertNotIn("source-in", source)


if __name__ == "__main__":
    main()
