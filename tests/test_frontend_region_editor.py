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

    def test_canvas_rendering_and_input_hardening(self):
        source = REGION_EDITOR.read_text(encoding="utf-8")

        # HiDPI backing store + coalesced redraws.
        self.assertIn("devicePixelRatio", source)
        self.assertIn("ResizeObserver", source)
        self.assertIn("requestAnimationFrame", source)
        self.assertNotIn("getComputedStyle", source)

        # Touch/pen: no scroll gestures on canvases, aborted strokes roll back.
        self.assertIn("touchAction: 'none'", source)
        self.assertIn("@pointercancel", source)

        # Ctrl+Z undo is wired, unreadable files surface an error.
        self.assertIn("if (canUndo.value) undo()", source)
        self.assertIn("无法读取该图片文件", source)


if __name__ == "__main__":
    main()
