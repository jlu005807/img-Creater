import io
import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import SkipTest, TestCase, main
from unittest.mock import patch

from PIL import Image

from backend.app import create_app


def _image_bytes(image_format="PNG", color=(32, 96, 160)):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format=image_format)
    return buffer.getvalue()


class SessionExportRouteTests(TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.result_dir = Path(self.tmp_dir.name) / "history"
        self.result_dir.mkdir()
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            IMAGE_SERVICE=SimpleNamespace(result_dir=self.result_dir),
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_session(self, directory_name, *, public_id=None, urls=None, files=None):
        session_dir = self.result_dir / directory_name
        session_dir.mkdir(parents=True)
        for filename, content in (files or {}).items():
            target = session_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if urls is None:
            urls = [f"/api/results/{directory_name}/{filename}" for filename in (files or {})]
        manifest = {
            "id": public_id if public_id is not None else directory_name,
            "prompt": "export test",
            "status": "completed",
            "urls": urls,
            "created_at": "2026-08-17T00:00:00Z",
            "updated_at": "2026-08-17T00:00:00Z",
        }
        (session_dir / "session.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        return session_dir

    def _post(self, items, **kwargs):
        return self.client.post("/api/sessions/export", json={"items": items}, **kwargs)

    @staticmethod
    def _zip(response):
        return zipfile.ZipFile(io.BytesIO(response.get_data()))

    def test_rejects_malformed_or_non_object_request_bodies(self):
        bodies = ["{broken", "[]", ""]
        responses = [
            self.client.post(
                "/api/sessions/export",
                data=bodies[0],
                content_type="application/json",
            ),
            self.client.post("/api/sessions/export", json=[]),
            self.client.post("/api/sessions/export", json=None),
        ]

        for body, response in zip(bodies, responses):
            with self.subTest(body=body):
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["success"])

    def test_rejects_invalid_root_and_item_schemas(self):
        invalid_payloads = [
            {},
            {"items": []},
            {"items": "session-1"},
            {"items": [], "url": "/api/results/session-1/image.png"},
            {"items": [None]},
            {"items": [{}]},
            {"items": [{"session_id": "session-1"}]},
            {"items": [{"image_index": 0}]},
            {"items": [{"session_id": "session-1", "image_index": 0, "url": "file:///secret"}]},
            {"items": [{"session_id": "", "image_index": 0}]},
            {"items": [{"session_id": 123, "image_index": 0}]},
            {"items": [{"session_id": "session-1", "image_index": True}]},
            {"items": [{"session_id": "session-1", "image_index": "0"}]},
            {"items": [{"session_id": "session-1", "image_index": -1}]},
            {
                "items": [
                    {"session_id": "session-1", "image_index": 0},
                    {"session_id": "session-1", "image_index": 0},
                ]
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/sessions/export", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["success"])

    def test_rejects_more_than_the_maximum_item_count(self):
        items = [{"session_id": f"session-{index}", "image_index": 0} for index in range(51)]

        response = self._post(items)

        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.get_json()["success"])

    def test_exports_real_images_for_legacy_public_ids_with_safe_unique_names(self):
        png = _image_bytes("PNG")
        jpeg = _image_bytes("JPEG", color=(160, 96, 32))
        self._create_session(
            "storage-one",
            public_id="legacy.id label",
            files={"first.png": png},
        )
        self._create_session(
            "storage-two",
            public_id="legacy id label",
            files={"second.jpg": jpeg},
        )
        items = [
            {"session_id": "legacy.id label", "image_index": 0},
            {"session_id": "legacy id label", "image_index": 0},
        ]

        response = self._post(items, headers={"Origin": "http://localhost:5173"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Export-Skipped-Count"], "0")
        self.assertIn(
            "X-Export-Skipped-Count",
            response.headers.get("Access-Control-Expose-Headers", ""),
        )
        disposition = response.headers["Content-Disposition"]
        self.assertRegex(disposition, r'^attachment; filename=img-Creater-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\.zip$')

        with self._zip(response) as archive:
            names = archive.namelist()
            self.assertEqual(names.count("export-report.json"), 1)
            image_names = [name for name in names if name != "export-report.json"]
            self.assertEqual(len(image_names), 2)
            self.assertEqual(len(set(image_names)), 2)
            self.assertTrue(all("/" not in name and "\\" not in name and ".." not in name for name in image_names))
            self.assertEqual({archive.read(name) for name in image_names}, {png, jpeg})
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))
            report = json.loads(archive.read("export-report.json"))

        self.assertEqual(report["skipped"], [])
        self.assertEqual(
            [(item["session_id"], item["image_index"]) for item in report["exported"]],
            [("legacy.id label", 0), ("legacy id label", 0)],
        )
        self.assertEqual({item["filename"] for item in report["exported"]}, set(image_names))

    def test_partial_export_includes_a_safe_report_and_skipped_header(self):
        png = _image_bytes()
        self._create_session(
            "mixed-session",
            files={"good.png": png},
            urls=[
                "/api/results/mixed-session/good.png",
                "https://cdn.example.com/private-token.png?token=secret",
            ],
        )

        response = self._post(
            [
                {"session_id": "mixed-session", "image_index": 0},
                {"session_id": "mixed-session", "image_index": 1},
            ]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Export-Skipped-Count"], "1")
        with self._zip(response) as archive:
            report = json.loads(archive.read("export-report.json"))
            self.assertEqual(len(report["exported"]), 1)
            self.assertEqual(len(report["skipped"]), 1)
            self.assertEqual(
                set(report["skipped"][0]),
                {"session_id", "image_index", "error"},
            )
            serialized = json.dumps(report, ensure_ascii=False)

        self.assertNotIn("cdn.example.com", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn(str(self.result_dir.resolve()), serialized)

    def test_all_failed_items_return_json_422_instead_of_an_empty_zip(self):
        self._create_session("remote-only", urls=["data:image/png;base64,AAAA"])

        response = self._post(
            [
                {"session_id": "remote-only", "image_index": 0},
                {"session_id": "missing-session", "image_index": 0},
            ]
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.mimetype, "application/json")
        body = response.get_json()
        self.assertFalse(body["success"])
        self.assertEqual(len(body["error"]["details"]["skipped"]), 2)
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("data:image", serialized)
        self.assertNotIn(str(self.result_dir.resolve()), serialized)

    def test_rejects_non_local_ambiguous_and_traversing_manifest_urls(self):
        good = _image_bytes()
        unsafe_urls = {
            "remote": "https://example.com/image.png",
            "data": "data:image/png;base64,AAAA",
            "absolute": str((self.result_dir / "secret.png").resolve()),
            "query": "/api/results/query/good.png?download=1",
            "fragment": "/api/results/fragment/good.png#preview",
            "traversal": "/api/results/traversal/%2e%2e/secret.png",
        }
        items = []
        for name, url in unsafe_urls.items():
            files = {"good.png": good} if name in {"query", "fragment"} else None
            self._create_session(name, urls=[url], files=files)
            items.append({"session_id": name, "image_index": 0})
        (self.result_dir / "secret.png").write_bytes(good)

        response = self._post(items)

        self.assertEqual(response.status_code, 422)
        skipped = response.get_json()["error"]["details"]["skipped"]
        self.assertEqual(len(skipped), len(items))
        serialized = json.dumps(skipped, ensure_ascii=False)
        for unsafe_fragment in ("example.com", "data:image", "secret.png", str(self.result_dir.resolve())):
            self.assertNotIn(unsafe_fragment, serialized)

    def test_manifest_cannot_reference_an_image_from_another_session(self):
        good = _image_bytes()
        self._create_session(
            "source-session",
            urls=["/api/results/target-session/image.png"],
        )
        self._create_session("target-session", files={"image.png": good})

        response = self._post([{"session_id": "source-session", "image_index": 0}])

        self.assertEqual(response.status_code, 422)

    def test_rejects_corrupt_images_and_extensions_that_disagree_with_the_real_format(self):
        png = _image_bytes("PNG")
        self._create_session("corrupt", files={"image.png": b"not an image"})
        self._create_session("mismatch", files={"image.jpg": png})

        response = self._post(
            [
                {"session_id": "corrupt", "image_index": 0},
                {"session_id": "mismatch", "image_index": 0},
            ]
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(response.get_json()["error"]["details"]["skipped"]), 2)

    def test_rejects_a_symbolic_link_that_escapes_the_session_directory(self):
        outside = self.result_dir / "outside.png"
        outside.write_bytes(_image_bytes())
        session_dir = self._create_session(
            "linked-session",
            urls=["/api/results/linked-session/link.png"],
        )
        try:
            (session_dir / "link.png").symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            raise SkipTest(f"symbolic links are unavailable: {exc}") from exc

        response = self._post([{"session_id": "linked-session", "image_index": 0}])

        self.assertEqual(response.status_code, 422)

    def test_returns_413_when_a_file_or_the_selection_exceeds_configured_byte_limits(self):
        first = _image_bytes("PNG", color=(1, 2, 3))
        second = _image_bytes("PNG", color=(4, 5, 6))
        self._create_session(
            "size-session",
            files={"first.png": first, "second.png": second},
        )
        items = [
            {"session_id": "size-session", "image_index": 0},
            {"session_id": "size-session", "image_index": 1},
        ]

        self.app.config["SESSION_EXPORT_MAX_FILE_BYTES"] = len(first) - 1
        file_too_large = self._post(items[:1])
        self.app.config["SESSION_EXPORT_MAX_FILE_BYTES"] = max(len(first), len(second)) + 1
        self.app.config["SESSION_EXPORT_MAX_TOTAL_BYTES"] = len(first) + len(second) - 1
        selection_too_large = self._post(items)

        self.assertEqual(file_too_large.status_code, 413)
        self.assertEqual(selection_too_large.status_code, 413)
        self.assertFalse(file_too_large.get_json()["success"])
        self.assertFalse(selection_too_large.get_json()["success"])

    def test_closes_the_spooled_archive_when_the_response_is_closed(self):
        self._create_session("cleanup", files={"image.png": _image_bytes()})
        original_factory = tempfile.SpooledTemporaryFile
        created = []

        def tracking_factory(*args, **kwargs):
            spool = original_factory(*args, **kwargs)
            created.append(spool)
            return spool

        with patch("backend.services.session_export_service.SpooledTemporaryFile", side_effect=tracking_factory):
            response = self._post([{"session_id": "cleanup", "image_index": 0}])
            response.get_data()
            response.close()

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


if __name__ == "__main__":
    main()
