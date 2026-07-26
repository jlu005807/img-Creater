from pathlib import Path
from unittest import TestCase, main


REGION_EDITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "RegionEditor" / "index.vue"
PLAYGROUND = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "Playground" / "index.vue"


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
        self.assertIn("if (canUndo.value && !drawing) undo()", source)
        self.assertIn("无法读取该图片文件", source)

    def test_mask_change_emission_is_lightweight_and_draft_is_pulled_on_demand(self):
        source = REGION_EDITOR.read_text(encoding="utf-8")

        # Per-stroke emissions must not PNG-encode the mask or embed the
        # base64 source image; the parent pulls exportDraft() on demand.
        emit_body = source.split("function emitMaskState()", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("exportDraft", emit_body)
        self.assertNotIn("toDataURL", emit_body)
        self.assertIn("maskRevision", emit_body)

        # exportDraft stays a public method and carries the image revision so
        # the parent can skip re-uploading an unchanged image.
        self.assertIn("defineExpose({ exportPayload, exportDraft, restoreDraft, clearMask, clearAll })", source)
        export_body = source.split("function exportDraft()", 1)[1].split("\n}", 1)[0]
        self.assertIn("imageRevision", export_body)


class PlaygroundEditDraftBufferTests(TestCase):
    def test_strokes_buffer_locally_and_completed_entries_stay_clean(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        # Strokes only mark the local buffer dirty behind a long debounce;
        # the heavy draft is pulled once via exportDraft() at flush time.
        self.assertIn("scheduleEditDraftFlush()", source)
        self.assertIn("EDIT_DRAFT_FLUSH_MS", source)
        self.assertNotRegex(source, r"nextState\??\.draft")
        # Completed entries are clone-on-edit: their stored draft is never
        # overwritten; the working draft attaches to the new entry at submit.
        self.assertIn("entry._status === 'completed'", source)

    def test_incremental_saves_skip_unchanged_source_image(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("savedDraftImageRevisions", source)
        self.assertIn("delete payload.image", source)


if __name__ == "__main__":
    main()
