from pathlib import Path
from unittest import TestCase, main


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "frontend" / "src" / "composables" / "useGenerationHistory.js"
PLAYGROUND = ROOT / "frontend" / "src" / "components" / "Playground" / "index.vue"


class ReferenceHistorySourceTests(TestCase):
    def test_persisted_sessions_restore_reference_images(self):
        history_source = HISTORY.read_text(encoding="utf-8")
        playground_source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("referenceImages: referenceImagesForSession(session)", history_source)
        self.assertIn("function referenceImagesForSession", history_source)
        self.assertIn("referenceImages.value = Array.isArray(entry.referenceImages)", playground_source)
        self.assertIn("referenceImages: persistedReferenceImages(result)", playground_source)
        self.assertIn("function persistedReferenceImages", playground_source)


if __name__ == "__main__":
    main()
