import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "frontend" / "src" / "components" / "Gallery" / "index.vue"
DOWNLOADS = ROOT / "frontend" / "src" / "utils" / "download.js"


class GalleryBatchDownloadContractTests(unittest.TestCase):
    def test_gallery_uses_stable_keys_for_cross_page_selection(self):
        source = GALLERY.read_text(encoding="utf-8")

        self.assertIn("const selectedKeys", source)
        self.assertIn("item.key", source)
        self.assertIn("toggleSelection", source)
        self.assertIn("selectLoadedImages", source)
        self.assertIn("clearSelection", source)
        self.assertIn("!hasMore.value", source)
        self.assertIn("reconcileSelection", source)

    def test_gallery_exposes_accessible_selection_toolbar_and_card_control(self):
        source = GALLERY.read_text(encoding="utf-8")

        self.assertIn("选择作品", source)
        self.assertIn("退出选择", source)
        self.assertIn("全选已加载", source)
        self.assertIn("清空", source)
        self.assertIn('type="checkbox"', source)
        self.assertIn("@click.stop", source)
        self.assertIn(":aria-label", source)
        self.assertIn(":aria-pressed=\"selectionMode\"", source)
        self.assertIn(":class=\"{ 'is-selected'", source)

    def test_gallery_orchestrates_progress_cancel_and_retryable_failures(self):
        source = GALLERY.read_text(encoding="utf-8")

        self.assertIn("downloadImagesAsZip", source)
        self.assertIn("MAX_BATCH_IMAGE_COUNT", source)
        self.assertIn("new AbortController()", source)
        self.assertIn("downloadProgress", source)
        self.assertIn("downloadProgress.completed", source)
        self.assertIn("downloadProgress.total", source)
        self.assertIn("downloadProgress.succeeded", source)
        self.assertIn("downloadProgress.failed", source)
        self.assertIn("cancelBatchDownload", source)
        self.assertIn("取消下载", source)
        self.assertIn("retryFailedDownloads", source)
        self.assertIn("失败", source)

    def test_manual_selection_changes_clear_a_stale_batch_summary(self):
        source = GALLERY.read_text(encoding="utf-8")
        toggle_start = source.index("function toggleSelection")
        toggle_end = source.index("\nfunction selectLoadedImages", toggle_start)

        self.assertIn("batchError.value = ''", source[toggle_start:toggle_end])

    def test_retry_prefers_the_current_selected_item_over_a_stale_failure_snapshot(self):
        source = GALLERY.read_text(encoding="utf-8")
        retry_start = source.index("async function retryFailedDownloads")
        retry_end = source.index("\nfunction cancelBatchDownload", retry_start)
        retry_source = source[retry_start:retry_end]

        self.assertIn("selectedItems.value.get(failure.key) || failure.item", retry_source)

    def test_batch_service_does_not_use_single_download_fallback(self):
        source = DOWNLOADS.read_text(encoding="utf-8")
        batch_start = source.index("export async function downloadImagesAsZip")
        batch_end = source.index("\n/**", batch_start)
        batch_source = source[batch_start:batch_end]

        self.assertNotIn("downloadImage(", batch_source)
        self.assertNotIn("window.open", batch_source)
        self.assertIn("triggerBlobDownload", source)
        self.assertIn("fetchImageBlob", source)
        self.assertIn("zip(files, { level: 6 }", source)


if __name__ == "__main__":
    unittest.main()
