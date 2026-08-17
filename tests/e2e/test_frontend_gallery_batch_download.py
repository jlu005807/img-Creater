import base64
import json
import os
import unittest
import zipfile
from urllib.parse import parse_qs, urlparse


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # Keep the default unit-test discovery usable without Playwright.
    sync_playwright = None


RUN_E2E = os.environ.get("RUN_FRONTEND_E2E") == "1"


@unittest.skipUnless(RUN_E2E and sync_playwright is not None, "set RUN_FRONTEND_E2E=1 with Playwright installed")
class GalleryBatchDownloadE2ETests(unittest.TestCase):
    """Browser contract for the two-page Gallery selection and ZIP workflow."""

    _png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "/A8AAusB9WnZ8X8AAAAASUVORK5CYII="
    )

    @classmethod
    def setUpClass(cls):
        cls.base_url = os.environ.get("FRONTEND_E2E_URL", "http://127.0.0.1:5173")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def _route_api(self, page):
        first_page = {
            "items": [
                {
                    "id": "session-a",
                    "prompt": "first page image",
                    "status": "completed",
                    "images": [{"index": 0, "url": "/api/results/session-a/image.png"}],
                },
            ],
            "next_cursor": "cursor-two",
            "has_more": True,
        }
        second_page = {
            "items": [
                {
                    "id": "session-b",
                    "prompt": "second page image",
                    "status": "completed",
                    "images": [{"index": 0, "url": "/api/results/session-b/image.png"}],
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }

        def route_api(route):
            request_url = urlparse(route.request.url)
            if request_url.path == "/api/sessions":
                query = parse_qs(request_url.query)
                payload = second_page if query.get("cursor") == ["cursor-two"] else first_page
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload),
                )
                return
            if request_url.path.startswith("/api/results/"):
                route.fulfill(status=200, content_type="image/png", body=self._png)
                return
            route.continue_()

        page.route("**/api/**", route_api)

    def test_cross_page_selection_and_single_zip_download(self):
        page = self.browser.new_page()
        downloads = []
        page.on("download", lambda download: downloads.append(download))
        try:
            self._route_api(page)
            page.goto(f"{self.base_url}/gallery")
            page.wait_for_load_state("networkidle")
            page.get_by_role("button", name="选择作品").click()

            checkboxes = page.locator('input[type="checkbox"]')
            checkboxes.nth(0).check()
            self.assertEqual(checkboxes.nth(0).is_checked(), True)

            scroll_root = page.locator(".thin-scrollbar").first
            scroll_root.evaluate("element => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event('scroll')); }")
            page.get_by_text("second page image").wait_for(timeout=10_000)

            # The first page selection remains checked after the cursor page is appended.
            self.assertEqual(checkboxes.nth(0).is_checked(), True)
            page.get_by_role("button", name="全选已加载").click()
            self.assertEqual(checkboxes.count(), 2)
            self.assertEqual(page.locator('input[type="checkbox"]:checked').count(), 2)
            self.assertIn("已选 2 张", page.locator("body").inner_text())

            with page.expect_download(timeout=15_000) as download_info:
                page.get_by_role("button", name="下载 ZIP").click()
            download = download_info.value
            download_path = download.path()
            self.assertIsNotNone(download_path)
            self.assertEqual(len(downloads), 1)

            with zipfile.ZipFile(download_path) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["session-a-image-1.png", "session-b-image-1.png"],
                )
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
