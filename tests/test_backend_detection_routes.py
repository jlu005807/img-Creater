import base64
import io
import sys
from unittest import TestCase, main

from backend.app import create_app


def _tiny_png_data_url() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (128, 64, 32)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class BackendDetectionRouteTests(TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def test_detect_health_reports_capability_state(self):
        response = self.client.get("/api/detect/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        data = payload["data"]
        self.assertIn("available", data)
        if data["available"]:
            self.assertIn("analyzers", data)
        else:
            self.assertTrue(data["missing_required"])

    def test_detect_missing_image_returns_400(self):
        response = self.client.post("/api/detect", json={})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("image", payload["error"]["message"])

    def test_detect_invalid_base64_returns_400(self):
        response = self.client.post("/api/detect", json={"image": "data:image/png;base64,@@not-base64@@"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_detect_oversized_payload_is_rejected(self):
        # >20MB decoded is >26MB as base64, which the app-level 25MB body cap
        # rejects first (413, same error envelope). Lift the body cap to also
        # exercise the route's own 20MB decoded guard (400).
        big = "data:image/png;base64," + base64.b64encode(b"\x00" * (21 * 1024 * 1024)).decode("ascii")

        capped = self.client.post("/api/detect", json={"image": big})
        self.assertEqual(capped.status_code, 413)
        self.assertFalse(capped.get_json()["success"])

        self.app.config["MAX_CONTENT_LENGTH"] = None
        uncapped = self.client.post("/api/detect", json={"image": big})
        self.assertEqual(uncapped.status_code, 400)
        payload = uncapped.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("过大", payload["error"]["message"])

    def test_detect_valid_image_matches_capability_state(self):
        health = self.client.get("/api/detect/health").get_json()["data"]

        response = self.client.post("/api/detect", json={"image": _tiny_png_data_url(), "filename": "tiny.png"})

        # With required deps missing the module still imports (pure stdlib), so
        # this is a 200 "unavailable" report rather than a 503.
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["available"], health["available"])
        if data["available"]:
            self.assertIn(data["verdict"], {"ai", "suspicious", "real"})
            self.assertIsInstance(data["stages"], dict)
        else:
            self.assertEqual(data["verdict"], "unavailable")
            self.assertTrue(data["missing_deps"])

    def test_detect_returns_503_when_module_import_fails(self):
        # Poison the import so `from detection import ...` raises, simulating a
        # broken/absent detection package (not merely missing numpy).
        saved = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "detection" or name.startswith("detection.")
        }
        sys.modules["detection"] = None
        try:
            post_response = self.client.post("/api/detect", json={"image": _tiny_png_data_url()})
            health_response = self.client.get("/api/detect/health")
        finally:
            sys.modules.pop("detection", None)
            sys.modules.update(saved)

        self.assertEqual(post_response.status_code, 503)
        payload = post_response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("检测模块未启用", payload["error"]["message"])

        # Health degrades gracefully instead of failing.
        self.assertEqual(health_response.status_code, 200)
        health = health_response.get_json()
        self.assertTrue(health["success"])
        self.assertFalse(health["data"]["available"])


if __name__ == "__main__":
    main()
