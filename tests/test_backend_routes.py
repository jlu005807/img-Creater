import base64
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote
from unittest import TestCase, main
from unittest.mock import patch

from backend.app import create_app
from backend.routes import generation as generation_routes
from backend.services.config_service import ConfigService


class FakeImageService:
    def __init__(self):
        self.submit_calls = []
        self.status_calls = []
        self.cancel_calls = []
        self.result_dir = None

    def submit_generation(self, prompt, size="1024x1024", n=1, quality=None, reference_images=None, history_id=None):
        self.submit_calls.append(
            {
                "prompt": prompt,
                "size": size,
                "n": n,
                "quality": quality,
                "reference_images": reference_images,
                "history_id": history_id,
            }
        )
        return {"task_id": "task-123", "status": "queued", "operation": "generate"}

    def submit_edit_generation(
        self,
        prompt,
        image=None,
        size="1024x1024",
        n=1,
        quality=None,
        history_id=None,
        source_image=None,
        marked_image=None,
        reference_images=None,
    ):
        self.submit_calls.append(
            {
                "prompt": prompt,
                "image": image,
                "source_image": source_image,
                "marked_image": marked_image,
                "reference_images": reference_images,
                "size": size,
                "n": n,
                "quality": quality,
                "history_id": history_id,
            }
        )
        return {"task_id": "edit-task-123", "status": "queued", "operation": "edit"}

    def poll_generation_status(self, api_id="", task_id=""):
        self.status_calls.append({"api_id": api_id, "task_id": task_id})
        return {
            "api_id": api_id or "api-1",
            "api_name": "Primary",
            "task_id": task_id,
            "operation": "generate",
            "status": "completed",
            "urls": ["https://cdn.example.com/image.png"],
            "attempts": [{"api_id": "api-1", "api_name": "Primary", "ok": True}],
            "expires_at": 1780309714,
            "error": None,
        }

    def cancel_generation(self, task_id=""):
        self.cancel_calls.append({"task_id": task_id})
        return {
            "task_id": task_id,
            "status": "cancelled",
            "error": "任务已手动停止",
        }


class BackendRouteTests(TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_service = ConfigService(config_path=Path(self.tmp_dir.name) / "configs.json")
        self.image_service = FakeImageService()
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            CONFIG_SERVICE=self.config_service,
            IMAGE_SERVICE=self.image_service,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_config_routes_create_list_update_reorder_and_delete_configs(self):
        primary_response = self.client.post(
            "/api/configs",
            json={
                "name": "Primary",
                "base_url": "https://primary.example.com/",
                "api_key": "key-1",
                "model": "gpt-image-2",
                "status": True,
            },
        )
        backup_response = self.client.post(
            "/api/configs",
            json={
                "name": "Backup",
                "base_url": "https://backup.example.com",
                "api_key": "key-2",
                "model": "gpt-image-2",
                "status": True,
            },
        )

        self.assertEqual(primary_response.status_code, 201)
        self.assertEqual(backup_response.status_code, 201)
        primary = primary_response.get_json()["data"]
        backup = backup_response.get_json()["data"]

        list_response = self.client.get("/api/configs")
        self.assertEqual([item["id"] for item in list_response.get_json()["data"]], [primary["id"], backup["id"]])

        update_response = self.client.put(f"/api/configs/{primary['id']}", json={"status": False})
        self.assertEqual(update_response.status_code, 200)
        self.assertFalse(update_response.get_json()["data"]["status"])

        reorder_response = self.client.post("/api/configs/reorder", json={"ordered_ids": [backup["id"], primary["id"]]})
        self.assertEqual(reorder_response.status_code, 200)
        self.assertEqual([item["id"] for item in reorder_response.get_json()["data"]], [backup["id"], primary["id"]])

        delete_response = self.client.delete(f"/api/configs/{backup['id']}")
        self.assertEqual(delete_response.status_code, 200)
        final_list_response = self.client.get("/api/configs")
        self.assertEqual([item["id"] for item in final_list_response.get_json()["data"]], [primary["id"]])

    def test_config_list_masks_api_key(self):
        self.client.post(
            "/api/configs",
            json={
                "name": "Primary",
                "base_url": "https://api.openai.com",
                "api_key": "sk-secret-1234",
                "model": "gpt-image-2",
                "status": True,
            },
        )
        data = self.client.get("/api/configs").get_json()["data"]
        self.assertEqual(len(data), 1)
        self.assertNotIn("api_key", data[0])
        self.assertEqual(data[0]["api_key_preview"], "••••1234")
        self.assertTrue(data[0]["has_api_key"])

    def test_config_secret_route_returns_raw_key(self):
        created = self.client.post(
            "/api/configs",
            json={
                "name": "Primary",
                "base_url": "https://api.openai.com",
                "api_key": "sk-secret-1234",
                "model": "gpt-image-2",
                "status": True,
            },
        ).get_json()["data"]

        secret = self.client.get(f"/api/configs/{created['id']}/secret")

        self.assertEqual(secret.status_code, 200)
        self.assertEqual(secret.get_json()["data"]["api_key"], "sk-secret-1234")

    def test_generation_routes_submit_and_poll_via_image_service(self):
        generate_response = self.client.post(
            "/api/generate",
            json={"prompt": "a red house", "size": "1024x1024", "n": 1},
        )
        self.assertEqual(generate_response.status_code, 202)
        self.assertEqual(generate_response.get_json()["data"]["task_id"], "task-123")
        self.assertEqual(
            self.image_service.submit_calls,
            [
                {
                    "prompt": "a red house",
                    "size": "1024x1024",
                    "n": 1,
                    "quality": None,
                    "reference_images": None,
                    "history_id": None,
                }
            ],
        )

        status_response = self.client.get("/api/status", query_string={"api_id": "api-1", "task_id": "task-123"})
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.get_json()["data"]["status"], "completed")
        self.assertEqual(self.image_service.status_calls, [{"api_id": "api-1", "task_id": "task-123"}])

    def test_generation_cancel_route_cancels_task_via_image_service(self):
        response = self.client.post("/api/tasks/task-123/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "cancelled")
        self.assertEqual(self.image_service.cancel_calls, [{"task_id": "task-123"}])

    def test_edit_route_submits_source_marked_and_reference_images(self):
        edit_response = self.client.post(
            "/api/edit",
            json={
                "prompt": "replace selected area",
                "source_image": "data:image/png;base64,c291cmNl",
                "marked_image": "data:image/png;base64,bWFya2Vk",
                "reference_images": ["data:image/png;base64,cmVm"],
                "size": "1024x1024",
                "n": 1,
            },
        )

        self.assertEqual(edit_response.status_code, 202)
        self.assertEqual(edit_response.get_json()["data"]["task_id"], "edit-task-123")
        self.assertEqual(
            self.image_service.submit_calls[-1],
            {
                "prompt": "replace selected area",
                "image": None,
                "source_image": "data:image/png;base64,c291cmNl",
                "marked_image": "data:image/png;base64,bWFya2Vk",
                "reference_images": ["data:image/png;base64,cmVm"],
                "size": "1024x1024",
                "n": 1,
                "quality": None,
                "history_id": None,
            },
        )

    def test_edit_draft_routes_persist_and_read_history_draft(self):
        self.image_service.result_dir = Path(self.tmp_dir.name) / "history"
        draft = {
            "fileName": "source.png",
            "image": "data:image/png;base64,aW1hZ2U=",
            "mask": "data:image/png;base64,bWFzaw==",
            "tool": "rect",
            "brushSize": 42,
        }

        save_response = self.client.put("/api/edit-drafts/history-1", json=draft)

        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(save_response.get_json()["data"]["history_id"], "history-1")
        draft_path = self.image_service.result_dir / "history-1" / "edit-draft.json"
        self.assertTrue(draft_path.exists())

        read_response = self.client.get("/api/edit-drafts/history-1")

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.get_json()["data"], draft)

    def test_edit_draft_incremental_put_merges_over_stored_full_draft(self):
        """PUT without image merges over the stored draft, preserving the original image.

        The merge happens server-side and the on-disk shape stays a complete
        draft, so GET reads old full drafts and merged drafts identically.
        """
        self.image_service.result_dir = Path(self.tmp_dir.name) / "history"
        full_draft = {
            "version": 1,
            "fileName": "source.png",
            "image": "data:image/png;base64,aW1hZ2U=",
            "imageRevision": 1,
            "mask": "data:image/png;base64,bWFzaw==",
            "tool": "brush",
            "brushSize": 42,
            "markerColor": "#ffffff",
        }
        first_response = self.client.put("/api/edit-drafts/history-1", json=full_draft)
        self.assertEqual(first_response.status_code, 200)

        incremental = {
            "version": 1,
            "image": "",
            "imageRevision": 1,
            "mask": "data:image/png;base64,bmV3bWFzaw==",
            "tool": "rect",
        }
        second_response = self.client.put("/api/edit-drafts/history-1", json=incremental)
        self.assertEqual(second_response.status_code, 200)

        merged = self.client.get("/api/edit-drafts/history-1").get_json()["data"]
        # Stored image survives an image-less save; empty image must not clobber it.
        self.assertEqual(merged["image"], "data:image/png;base64,aW1hZ2U=")
        self.assertEqual(merged["mask"], "data:image/png;base64,bmV3bWFzaw==")
        self.assertEqual(merged["tool"], "rect")
        # Fields the incremental payload omitted survive the merge.
        self.assertEqual(merged["fileName"], "source.png")
        self.assertEqual(merged["brushSize"], 42)
        self.assertEqual(merged["markerColor"], "#ffffff")

    def test_edit_draft_incremental_put_without_existing_draft_stores_payload_as_is(self):
        """An image-less PUT with no draft on disk stores the payload as sent.

        Chosen policy: 200 + store-as-is (not 400) for forward-compat with
        clients that legitimately track image-less drafts; restore treats a
        draft without an image as empty, so nothing breaks downstream.
        """
        self.image_service.result_dir = Path(self.tmp_dir.name) / "history"

        response = self.client.put(
            "/api/edit-drafts/history-slim",
            json={"version": 1, "mask": "data:image/png;base64,bWFzaw=="},
        )

        self.assertEqual(response.status_code, 200)
        stored = self.client.get("/api/edit-drafts/history-slim").get_json()["data"]
        self.assertEqual(stored, {"version": 1, "mask": "data:image/png;base64,bWFzaw=="})

    def test_missing_edit_draft_returns_null(self):
        self.image_service.result_dir = Path(self.tmp_dir.name) / "history"

        response = self.client.get("/api/edit-drafts/missing")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["data"])

    def test_prompt_template_routes_persist_update_and_delete_templates(self):
        templates_path = Path(self.tmp_dir.name) / "prompt_templates.json"
        self.app.config["PROMPT_TEMPLATE_PATH"] = templates_path

        create_response = self.client.post(
            "/api/prompt-templates",
            json={"title": "Product", "text": "clean product photo"},
        )

        self.assertEqual(create_response.status_code, 201)
        created = create_response.get_json()["data"]
        self.assertEqual(created["title"], "Product")
        self.assertEqual(created["text"], "clean product photo")
        self.assertTrue(templates_path.exists())

        list_response = self.client.get("/api/prompt-templates")
        self.assertEqual(list_response.status_code, 200)
        listed = list_response.get_json()["data"]
        self.assertEqual(listed[0]["id"], created["id"])

        update_response = self.client.put(
            f"/api/prompt-templates/{created['id']}",
            json={"title": "Updated", "text": "updated prompt"},
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.get_json()["data"]["title"], "Updated")

        delete_response = self.client.delete(f"/api/prompt-templates/{created['id']}")
        self.assertEqual(delete_response.status_code, 200)
        final_list = self.client.get("/api/prompt-templates").get_json()["data"]
        self.assertNotIn(created["id"], [item["id"] for item in final_list])

    def test_sessions_route_lists_persisted_completed_images(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        session_dir = result_dir / "history-1"
        session_dir.mkdir(parents=True)
        (session_dir / "image.png").write_bytes(b"image")
        (session_dir / "session.json").write_text(
            (
                '{"id":"history-1","prompt":"a red house","mode":"generate",'
                '"size":"1024x1024","status":"completed","urls":["/api/results/history-1/image.png"],'
                '"reference_images":["/api/results/history-1/references/ref.png"],'
                '"created_at":"2026-06-08T00:00:00Z","updated_at":"2026-06-08T00:00:01Z"}'
            ),
            encoding="utf-8",
        )

        response = self.client.get("/api/sessions")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "history-1")
        self.assertEqual(data[0]["images"][0]["url"], "/api/results/history-1/image.png")
        self.assertEqual(data[0]["reference_images"], ["/api/results/history-1/references/ref.png"])
        self.assertEqual(data[0]["prompt"], "a red house")

    def test_result_file_route_serves_reference_subdirectory(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        reference_dir = result_dir / "history-1" / "references"
        reference_dir.mkdir(parents=True)
        (result_dir / "history-1" / "image.png").write_bytes(b"result-bytes")
        (reference_dir / "ref-0-abcd1234.png").write_bytes(b"reference-bytes")

        image_response = self.client.get("/api/results/history-1/image.png")
        reference_response = self.client.get("/api/results/history-1/references/ref-0-abcd1234.png")

        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.data, b"result-bytes")
        # The exact URL shape persisted by _persist_reference_images must be
        # servable end-to-end, not just stored in the manifest.
        self.assertEqual(reference_response.status_code, 200)
        self.assertEqual(reference_response.data, b"reference-bytes")

    def test_result_file_route_rejects_traversal_history_ids(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        result_dir.mkdir(parents=True)
        secret = Path(self.tmp_dir.name) / "secret.txt"
        secret.write_text("api-keys", encoding="utf-8")

        response = self.client.get("/api/results/%2e%2e/secret.txt")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"api-keys", response.data)

    def test_sessions_route_accepts_utf8_bom_manifests(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        session_dir = result_dir / "history-bom"
        session_dir.mkdir(parents=True)
        (session_dir / "image.png").write_bytes(b"image")
        (session_dir / "session.json").write_text(
            (
                '\ufeff{"id":"history-bom","prompt":"bom manifest","mode":"generate",'
                '"status":"completed","urls":["/api/results/history-bom/image.png"],'
                '"created_at":"2026-06-08T00:00:00Z","updated_at":"2026-06-08T00:00:01Z"}'
            ),
            encoding="utf-8",
        )

        response = self.client.get("/api/sessions")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "history-bom")

    def test_delete_session_routes_remove_persisted_history_dirs(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        for name in ["history-1", "history-2"]:
            session_dir = result_dir / name
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text('{"status":"completed","urls":["/x.png"]}', encoding="utf-8")

        single_response = self.client.delete("/api/sessions/history-1")

        self.assertEqual(single_response.status_code, 200)
        self.assertFalse((result_dir / "history-1").exists())
        self.assertTrue((result_dir / "history-2").exists())

        all_response = self.client.delete("/api/sessions")

        self.assertEqual(all_response.status_code, 200)
        self.assertFalse((result_dir / "history-2").exists())

class SessionPaginationContractTests(TestCase):
    """Stage 0 contract tests for the planned pagination API."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_service = ConfigService(config_path=Path(self.tmp_dir.name) / "configs.json")
        self.image_service = FakeImageService()
        self.app = create_app()
        self.app.config.update(TESTING=True, CONFIG_SERVICE=self.config_service, IMAGE_SERVICE=self.image_service)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_session(self, result_dir, name, prompt="test", updated_at=None, created_at=None):
        session_dir = result_dir / name
        session_dir.mkdir(parents=True)
        (session_dir / "image.png").write_bytes(b"image")
        manifest = {
            "id": name, "prompt": prompt, "mode": "generate", "size": "1024x1024",
            "status": "completed", "urls": [f"/api/results/{name}/image.png"],
            "created_at": created_at or f"2026-01-01T00:00:0{name[-1]}Z",
            "updated_at": updated_at or f"2026-01-01T00:00:0{name[-1]}Z",
        }
        (session_dir / "session.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest

    def test_legacy_no_params_returns_array(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        self._create_session(result_dir, "h-1")
        response = self.client.get("/api/sessions")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_pagination_response_shape(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        for i in range(5):
            self._create_session(result_dir, f"h-{i}", updated_at=f"2026-01-01T00:00:0{i}Z", created_at=f"2026-01-01T00:00:0{i}Z")
        response = self.client.get("/api/sessions?limit=2")
        data = response.get_json()["data"]
        self.assertIsInstance(data, dict)
        self.assertIn("items", data)
        self.assertIn("has_more", data)
        self.assertIn("next_cursor", data)
        self.assertIsInstance(data["items"], list)
        self.assertLessEqual(len(data["items"]), 2)
        self.assertTrue(data["has_more"])

    def test_supported_param_uses_default_limit_of_thirty(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamp = "2026-01-01T00:00:00Z"
        for i in range(31):
            self._create_session(
                result_dir,
                f"h-{i:02d}",
                updated_at=timestamp,
                created_at=timestamp,
            )

        first = self.client.get("/api/sessions", query_string={"q": "test"}).get_json()["data"]

        self.assertEqual(len(first["items"]), 30)
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])

    def test_empty_result_dir_preserves_legacy_and_paginated_shapes(self):
        self.image_service.result_dir = Path(self.tmp_dir.name) / "missing-history"

        legacy = self.client.get("/api/sessions").get_json()["data"]
        paginated = self.client.get("/api/sessions?limit=30").get_json()["data"]

        self.assertEqual(legacy, [])
        self.assertEqual(paginated, {"items": [], "next_cursor": None, "has_more": False})

    def test_pagination_cursor_stability(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        for i in range(6):
            self._create_session(result_dir, f"h-{i}", updated_at=f"2026-01-01T00:00:0{i}Z", created_at=f"2026-01-01T00:00:0{i}Z")
        first = self.client.get("/api/sessions?limit=3")
        first_data = first.get_json()["data"]
        self.assertTrue(first_data["has_more"])
        cursor = first_data["next_cursor"]
        self.assertIsNotNone(cursor)
        second = self.client.get(f"/api/sessions?limit=3&cursor={cursor}")
        second_data = second.get_json()["data"]
        first_ids = {item["id"] for item in first_data["items"]}
        second_ids = {item["id"] for item in second_data["items"]}
        self.assertEqual(first_ids & second_ids, set(), "Pages should not overlap")

    def test_cursor_uses_id_tiebreaker_for_equal_timestamps(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamp = "2026-01-01T00:00:00Z"
        for session_id in ["h-a", "h-b", "h-c", "h-d"]:
            self._create_session(result_dir, session_id, updated_at=timestamp, created_at=timestamp)

        first = self.client.get("/api/sessions?limit=2").get_json()["data"]
        second = self.client.get(
            "/api/sessions", query_string={"limit": 2, "cursor": first["next_cursor"]}
        ).get_json()["data"]

        self.assertEqual([item["id"] for item in first["items"]], ["h-d", "h-c"])
        self.assertEqual([item["id"] for item in second["items"]], ["h-b", "h-a"])
        self.assertFalse(second["has_more"])

    def test_q_filters_prompt_case_insensitively(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        self._create_session(result_dir, "h-1", prompt="Sunset Over Water")
        self._create_session(result_dir, "h-2", prompt="Forest trail")

        response = self.client.get("/api/sessions", query_string={"q": "sUnSeT"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertIsInstance(data, dict)
        self.assertEqual([item["id"] for item in data["items"]], ["h-1"])

    def test_from_and_to_filters_are_inclusive(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamps = {
            "h-0": "2026-01-01T00:00:00Z",
            "h-1": "2026-01-01T00:00:01Z",
            "h-2": "2026-01-01T00:00:02Z",
            "h-3": "2026-01-01T00:00:03Z",
            "h-4": "2026-01-01T00:00:04Z",
        }
        for session_id, timestamp in timestamps.items():
            self._create_session(result_dir, session_id, updated_at=timestamp, created_at=timestamp)

        response = self.client.get(
            "/api/sessions",
            query_string={"from": timestamps["h-1"], "to": timestamps["h-3"]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual([item["id"] for item in data["items"]], ["h-3", "h-2", "h-1"])

    def test_invalid_date_filters_return_400(self):
        invalid = self.client.get("/api/sessions", query_string={"from": "not-a-date"})
        utc_overflow = self.client.get(
            "/api/sessions", query_string={"from": "0001-01-01T00:00:00+23:59"}
        )
        reversed_range = self.client.get(
            "/api/sessions",
            query_string={"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(utc_overflow.status_code, 400)
        self.assertEqual(reversed_range.status_code, 400)

    def test_unsupported_sort_returns_400(self):
        response = self.client.get("/api/sessions", query_string={"sort": "created_desc"})

        self.assertEqual(response.status_code, 400)

    def test_bad_manifest_is_skipped_not_500(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        good_dir = result_dir / "good-1"
        good_dir.mkdir(parents=True)
        (good_dir / "image.png").write_bytes(b"image")
        (good_dir / "session.json").write_text(json.dumps({"id": "good-1", "prompt": "ok", "mode": "generate", "size": "1024x1024", "status": "completed", "urls": ["/api/results/good-1/image.png"], "created_at": "2026-01-01T00:00:01Z", "updated_at": "2026-01-01T00:00:01Z"}, ensure_ascii=False), encoding="utf-8")
        bad_dir = result_dir / "bad-1"
        bad_dir.mkdir(parents=True)
        (bad_dir / "image.png").write_bytes(b"image")
        (bad_dir / "session.json").write_text("NOT VALID JSON {{{", encoding="utf-8")
        with self.assertLogs(self.app.logger.name, level="WARNING") as captured:
            response = self.client.get("/api/sessions")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertIsInstance(data, list)
        ids = [item["id"] for item in data]
        self.assertIn("good-1", ids)
        self.assertNotIn("bad-1", ids)
        warning_text = "\n".join(captured.output)
        self.assertIn("bad-1", warning_text)
        self.assertIn("JSONDecodeError", warning_text)
        self.assertNotIn(str(result_dir.resolve()), warning_text)

    def test_manifest_permission_and_concurrent_removal_errors_are_isolated(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        self._create_session(result_dir, "good-1")
        self._create_session(result_dir, "permission-denied")
        self._create_session(result_dir, "removed-during-read")
        permission_manifest = result_dir / "permission-denied" / "session.json"
        removed_manifest = result_dir / "removed-during-read" / "session.json"
        original_open = Path.open

        def open_manifest(path, *args, **kwargs):
            if path == permission_manifest:
                raise PermissionError("denied for test")
            if path == removed_manifest:
                raise FileNotFoundError("removed for test")
            return original_open(path, *args, **kwargs)

        with self.assertLogs(self.app.logger.name, level="WARNING") as captured:
            with patch.object(Path, "open", autospec=True, side_effect=open_manifest):
                response = self.client.get("/api/sessions?limit=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.get_json()["data"]["items"]],
            ["good-1"],
        )
        warning_text = "\n".join(captured.output)
        self.assertIn("permission-denied", warning_text)
        self.assertIn("PermissionError", warning_text)
        self.assertIn("removed-during-read", warning_text)
        self.assertIn("FileNotFoundError", warning_text)
        self.assertNotIn(str(result_dir.resolve()), warning_text)

    def test_history_directory_enumeration_errors_do_not_fail_the_page(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        result_dir.mkdir(parents=True)
        self.image_service.result_dir = result_dir
        original_iterdir = Path.iterdir

        def iterdir_with_permission_error(path):
            if path == result_dir:
                raise PermissionError("history root denied for test")
            return original_iterdir(path)

        with self.assertLogs(self.app.logger.name, level="WARNING") as captured:
            with patch.object(Path, "iterdir", autospec=True, side_effect=iterdir_with_permission_error):
                response = self.client.get("/api/sessions?limit=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["items"], [])
        warning_text = "\n".join(captured.output)
        self.assertIn("history directory", warning_text)
        self.assertIn("PermissionError", warning_text)
        self.assertNotIn(str(result_dir.resolve()), warning_text)

    def test_legacy_timestamp_variants_use_created_or_directory_mtime(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        manifests = {
            "no-timestamps": {
                "id": "no-timestamps",
                "prompt": "legacy without timestamps",
                "status": "completed",
                "urls": ["/api/results/no-timestamps/image.png"],
            },
            "invalid-updated": {
                "id": "invalid-updated",
                "prompt": "created timestamp survives",
                "status": "completed",
                "urls": ["/api/results/invalid-updated/image.png"],
                "updated_at": "not-a-timestamp",
                "created_at": "2026-01-01T00:00:00Z",
            },
            "updated-wins": {
                "id": "updated-wins",
                "prompt": "updated timestamp wins",
                "status": "completed",
                "urls": ["/api/results/updated-wins/image.png"],
                "updated_at": "2027-01-01T00:00:00Z",
                "created_at": "2025-01-01T00:00:00Z",
            },
            "malformed-timestamps": {
                "id": "malformed-timestamps",
                "prompt": "mtime fallback survives",
                "status": "completed",
                "urls": ["/api/results/malformed-timestamps/image.png"],
                "updated_at": "still-not-a-timestamp",
                "created_at": "also-not-a-timestamp",
            },
        }
        mtime_epoch = 1_735_689_600  # 2025-01-01T00:00:00Z
        for session_id, manifest in manifests.items():
            session_dir = result_dir / session_id
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(json.dumps(manifest), encoding="utf-8")
            os.utime(session_dir, (mtime_epoch, mtime_epoch))

        legacy = self.client.get("/api/sessions")
        paginated = self.client.get("/api/sessions?limit=10")

        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(paginated.status_code, 200)
        legacy_ids = {item["id"] for item in legacy.get_json()["data"]}
        paginated_ids = {item["id"] for item in paginated.get_json()["data"]["items"]}
        expected_ids = set(manifests)
        self.assertEqual(legacy_ids, expected_ids)
        self.assertEqual(paginated_ids, expected_ids)
        summaries = {
            item["id"]: item
            for item in paginated.get_json()["data"]["items"]
        }
        self.assertEqual(summaries["invalid-updated"]["updated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(summaries["no-timestamps"]["updated_at"], "2025-01-01T00:00:00Z")
        self.assertEqual(summaries["malformed-timestamps"]["updated_at"], "2025-01-01T00:00:00Z")

        # The valid created_at value wins when updated_at is malformed.
        created_filter = self.client.get(
            "/api/sessions",
            query_string={"from": "2025-12-31T23:59:59Z", "to": "2026-01-01T00:00:01Z"},
        )
        self.assertEqual(
            [item["id"] for item in created_filter.get_json()["data"]["items"]],
            ["invalid-updated"],
        )
        updated_filter = self.client.get(
            "/api/sessions",
            query_string={"from": "2026-12-31T23:59:59Z", "to": "2027-01-01T00:00:01Z"},
        )
        self.assertEqual(
            [item["id"] for item in updated_filter.get_json()["data"]["items"]],
            ["updated-wins"],
        )

    def test_directory_mtime_and_id_form_a_stable_cross_page_cursor(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        mtimes = {
            "mtime-a": 1_735_689_600,
            "mtime-b": 1_735_689_601,
            "mtime-c": 1_735_689_602,
            "mtime-d": 1_735_689_602,
        }
        for session_id, mtime in mtimes.items():
            session_dir = result_dir / session_id
            session_dir.mkdir(parents=True)
            manifest = {
                "id": session_id,
                "prompt": "mtime fallback",
                "status": "completed",
                "urls": [f"/api/results/{session_id}/image.png"],
            }
            (session_dir / "session.json").write_text(json.dumps(manifest), encoding="utf-8")
            os.utime(session_dir, (mtime, mtime))

        first = self.client.get("/api/sessions?limit=2").get_json()["data"]
        second = self.client.get(
            "/api/sessions",
            query_string={"limit": 2, "cursor": first["next_cursor"]},
        ).get_json()["data"]

        self.assertEqual([item["id"] for item in first["items"]], ["mtime-d", "mtime-c"])
        self.assertEqual([item["id"] for item in second["items"]], ["mtime-b", "mtime-a"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])

    def test_duplicate_public_ids_are_deduplicated_before_cursor_paging(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamp = "2026-01-01T00:00:00Z"
        manifests = {
            "a-directory": {"id": "duplicate", "prompt": "keep first", "urls": ["/a.png"]},
            "b-directory": {"id": "duplicate", "prompt": "drop second", "urls": ["/b.png"]},
            "c-directory": {"id": "unique-c", "prompt": "unique c", "urls": ["/c.png"]},
            "d-directory": {"id": "unique-d", "prompt": "unique d", "urls": ["/d.png"]},
        }
        for directory_name, values in manifests.items():
            session_dir = result_dir / directory_name
            session_dir.mkdir(parents=True)
            manifest = {
                **values,
                "status": "completed",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            (session_dir / "session.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertLogs(self.app.logger.name, level="WARNING") as captured:
            first = self.client.get("/api/sessions?limit=2").get_json()["data"]
            second = self.client.get(
                "/api/sessions", query_string={"limit": 2, "cursor": first["next_cursor"]}
            ).get_json()["data"]

        first_ids = [item["id"] for item in first["items"]]
        second_ids = [item["id"] for item in second["items"]]
        self.assertEqual(first_ids, ["unique-d", "unique-c"])
        self.assertEqual(second_ids, ["duplicate"])
        self.assertEqual(set(first_ids) & set(second_ids), set())
        self.assertFalse(second["has_more"])
        self.assertEqual(
            next(item["prompt"] for item in second["items"] if item["id"] == "duplicate"),
            "keep first",
        )
        warning_text = "\n".join(captured.output)
        self.assertIn("duplicate", warning_text)
        self.assertIn("b-directory", warning_text)
        self.assertNotIn(str(result_dir.resolve()), warning_text)

        detail = self.client.get("/api/sessions/duplicate")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["data"]["prompt"], "keep first")

    def test_unexpected_manifest_loader_errors_surface_as_500(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        self._create_session(result_dir, "h-1")

        with self.assertLogs(self.app.logger.name, level="ERROR") as captured:
            with patch.object(generation_routes, "_session_record", side_effect=RuntimeError("programmer bug")):
                response = self.client.get("/api/sessions")

        self.assertEqual(response.status_code, 500)
        self.assertTrue(any("Unhandled generation route error" in line for line in captured.output))

    def test_paginated_summary_replaces_non_finite_scalars_with_null(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        session_dir = result_dir / "non-finite"
        session_dir.mkdir(parents=True)
        manifest = {
            "id": "non-finite",
            "prompt": "scalar safety",
            "status": "completed",
            "urls": ["/api/results/non-finite/image.png"],
            "mode": float("nan"),
            "n": float("inf"),
            "expires_at": float("-inf"),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        (session_dir / "session.json").write_text(json.dumps(manifest), encoding="utf-8")

        response = self.client.get("/api/sessions?limit=10")

        self.assertEqual(response.status_code, 200)
        item = response.get_json()["data"]["items"][0]
        self.assertIsNone(item["mode"])
        self.assertIsNone(item["n"])
        self.assertIsNone(item["expires_at"])
        body = response.get_data(as_text=True)
        self.assertNotIn("NaN", body)
        self.assertNotIn("Infinity", body)

    def test_paginated_prompt_is_capped_but_q_filters_full_prompt(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        long_prompt = "x" * 4000 + " needle beyond summary cap"
        self._create_session(
            result_dir,
            "long-prompt",
            prompt=long_prompt,
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )

        paginated = self.client.get("/api/sessions?limit=10").get_json()["data"]
        filtered = self.client.get("/api/sessions", query_string={"q": "needle"}).get_json()["data"]
        legacy = self.client.get("/api/sessions").get_json()["data"]

        self.assertEqual(len(paginated["items"][0]["prompt"]), 4000)
        self.assertEqual([item["id"] for item in filtered["items"]], ["long-prompt"])
        self.assertEqual(legacy[0]["prompt"], long_prompt)

    def test_session_detail_returns_the_full_manifest_for_parameter_reuse(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        long_prompt = "x" * 4000 + " complete detail suffix"
        manifest = self._create_session(
            result_dir,
            "detail-1",
            prompt=long_prompt,
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )
        manifest["attempts"] = [{"api_id": "api-1", "ok": True}]
        manifest["response_meta"] = {"request_id": "request-1"}
        (result_dir / "detail-1" / "session.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        response = self.client.get("/api/sessions/detail-1")
        missing = self.client.get("/api/sessions/missing-detail")

        self.assertEqual(response.status_code, 200)
        detail = response.get_json()["data"]
        self.assertEqual(detail["prompt"], long_prompt)
        self.assertEqual(detail["attempts"], manifest["attempts"])
        self.assertEqual(detail["response_meta"], manifest["response_meta"])
        self.assertEqual(detail["images"][0]["session_id"], "detail-1")
        self.assertEqual(missing.status_code, 404)

    def test_session_detail_hides_unreadable_manifest_paths(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        session_dir = result_dir / "broken-detail"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text("{broken", encoding="utf-8")

        with self.assertLogs("backend.app", level="WARNING") as logs:
            response = self.client.get("/api/sessions/broken-detail")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(session_dir), "\n".join(logs.output))
        self.assertIn("broken-detail", "\n".join(logs.output))

    def test_session_detail_resolves_the_public_id_returned_by_the_list(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        manifest = self._create_session(
            result_dir,
            "storage-name",
            prompt="public detail",
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )
        manifest["id"] = "public-detail-id"
        (result_dir / "storage-name" / "session.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        listed = self.client.get("/api/sessions?limit=10").get_json()["data"]["items"]
        response = self.client.get(f"/api/sessions/{listed[0]['id']}")

        self.assertEqual(listed[0]["id"], "public-detail-id")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["id"], "public-detail-id")

    def test_session_delete_resolves_the_public_id_returned_by_the_list(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        manifest = self._create_session(
            result_dir,
            "storage-name",
            prompt="public delete",
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )
        manifest["id"] = "public-delete-id"
        (result_dir / "storage-name" / "session.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        listed = self.client.get("/api/sessions?limit=10").get_json()["data"]["items"]
        response = self.client.delete(f"/api/sessions/{listed[0]['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse((result_dir / "storage-name").exists())

    def test_session_delete_uses_the_deterministic_directory_for_duplicate_public_ids(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamp = "2026-01-01T00:00:00Z"
        for directory_name, prompt in (("a-directory", "keep first"), ("b-directory", "keep second")):
            session_dir = result_dir / directory_name
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(
                json.dumps({
                    "id": "duplicate-delete",
                    "prompt": prompt,
                    "status": "completed",
                    "urls": [f"/api/results/{directory_name}/image.png"],
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }),
                encoding="utf-8",
            )

        response = self.client.delete("/api/sessions/duplicate-delete")

        self.assertEqual(response.status_code, 200)
        self.assertFalse((result_dir / "a-directory").exists())
        self.assertTrue((result_dir / "b-directory").exists())

    def test_session_detail_and_delete_match_raw_legacy_public_ids_before_normalizing(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        raw_public_id = "legacy.id label"
        manifest = self._create_session(
            result_dir,
            "legacy-storage",
            prompt="raw public id",
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )
        manifest["id"] = raw_public_id
        (result_dir / "legacy-storage" / "session.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

        listed = self.client.get("/api/sessions?limit=10").get_json()["data"]["items"]
        detail = self.client.get(f"/api/sessions/{raw_public_id}")
        deleted = self.client.delete(f"/api/sessions/{raw_public_id}")

        self.assertEqual(listed[0]["id"], raw_public_id)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["data"]["id"], raw_public_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse((result_dir / "legacy-storage").exists())

    def test_session_detail_and_delete_accept_encoded_reserved_legacy_public_ids(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        raw_public_id = "legacy?id#part/segment"
        manifest = self._create_session(
            result_dir,
            "reserved-storage",
            prompt="reserved raw public id",
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )
        manifest["id"] = raw_public_id
        (result_dir / "reserved-storage" / "session.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        encoded_path = quote(raw_public_id, safe="")

        detail = self.client.get(f"/api/sessions/{encoded_path}")
        deleted = self.client.delete(f"/api/sessions/{encoded_path}")

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["data"]["id"], raw_public_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse((result_dir / "reserved-storage").exists())

    def test_session_delete_does_not_follow_a_normalized_legacy_id_collision(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        raw_public_id = "public.id"
        manifest = self._create_session(
            result_dir,
            "unsafe-storage",
            prompt="raw public id must win",
            updated_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
        )
        manifest["id"] = raw_public_id
        (result_dir / "unsafe-storage" / "session.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        collision_dir = result_dir / "public-id"
        collision_dir.mkdir(parents=True)

        response = self.client.delete(f"/api/sessions/{raw_public_id}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse((result_dir / "unsafe-storage").exists())
        self.assertTrue(collision_dir.exists())

    def test_session_delete_does_not_fallback_to_a_normalized_collision_for_unknown_raw_id(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        collision_dir = result_dir / "missing-id"
        collision_dir.mkdir(parents=True)

        response = self.client.delete("/api/sessions/missing.id")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(collision_dir.exists())

    def test_invalid_completed_manifests_warn_and_blank_urls_are_filtered(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamp = "2026-01-01T00:00:01Z"
        manifests = {
            "mixed-urls": {
                "id": "mixed-urls",
                "prompt": "valid strings survive",
                "status": "completed",
                "urls": [123, "", "/api/results/mixed-urls/image.png", "   ", None],
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            "missing-urls": {
                "id": "missing-urls",
                "status": "completed",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            "no-string-urls": {
                "id": "no-string-urls",
                "status": "completed",
                "urls": [123, None],
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            "blank-urls": {
                "id": "blank-urls",
                "status": "completed",
                "urls": ["", "   "],
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        }
        for session_id, manifest in manifests.items():
            session_dir = result_dir / session_id
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertLogs(self.app.logger.name, level="WARNING") as captured:
            response = self.client.get("/api/sessions?limit=10")

        self.assertEqual(response.status_code, 200)
        items = response.get_json()["data"]["items"]
        self.assertEqual([item["id"] for item in items], ["mixed-urls"])
        self.assertEqual(items[0]["urls"], ["/api/results/mixed-urls/image.png"])
        self.assertEqual(items[0]["images"][0]["index"], 0)
        warnings = "\n".join(captured.output)
        self.assertIn("missing-urls", warnings)
        self.assertIn("no-string-urls", warnings)
        self.assertIn("blank-urls", warnings)

        legacy = self.client.get("/api/sessions").get_json()["data"]
        self.assertEqual([item["id"] for item in legacy], ["mixed-urls"])

    def test_only_completed_sessions_with_images_are_listed(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        timestamp = "2026-01-01T00:00:00Z"
        for status in ["completed", "queued", "processing", "failed", "cancelled"]:
            session_id = f"status-{status}"
            session_dir = result_dir / session_id
            session_dir.mkdir(parents=True)
            manifest = {
                "id": session_id,
                "prompt": status,
                "status": status,
                "urls": [f"/api/results/{session_id}/image.png"],
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            (session_dir / "session.json").write_text(json.dumps(manifest), encoding="utf-8")

        legacy = self.client.get("/api/sessions").get_json()["data"]
        paginated = self.client.get("/api/sessions?limit=10").get_json()["data"]

        self.assertEqual([item["id"] for item in legacy], ["status-completed"])
        self.assertEqual([item["id"] for item in paginated["items"]], ["status-completed"])

    def test_limit_out_of_range_returns_400(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        self._create_session(result_dir, "h-1")
        resp_zero = self.client.get("/api/sessions?limit=0")
        self.assertEqual(resp_zero.status_code, 400)
        resp_huge = self.client.get("/api/sessions?limit=101")
        self.assertEqual(resp_huge.status_code, 400)
        resp_non_integer = self.client.get("/api/sessions?limit=1.5")
        self.assertEqual(resp_non_integer.status_code, 400)

    def test_invalid_cursor_returns_400(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        self._create_session(result_dir, "h-1")
        response = self.client.get("/api/sessions?limit=10&cursor=!!!invalid!!!")
        overflow_payload = json.dumps(["0001-01-01T00:00:00+23:59", "h-1"], separators=(",", ":"))
        overflow_cursor = base64.urlsafe_b64encode(overflow_payload.encode("utf-8")).decode("ascii").rstrip("=")
        overflow_response = self.client.get(
            "/api/sessions", query_string={"limit": 10, "cursor": overflow_cursor}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(overflow_response.status_code, 400)

    def test_last_page_has_no_more(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        for i in range(3):
            self._create_session(result_dir, f"h-{i}", updated_at=f"2026-01-01T00:00:0{i}Z", created_at=f"2026-01-01T00:00:0{i}Z")
        response = self.client.get("/api/sessions?limit=10")
        data = response.get_json()["data"]
        self.assertFalse(data["has_more"])
        self.assertIsNone(data["next_cursor"])

    def test_summary_response_omits_large_fields(self):
        result_dir = Path(self.tmp_dir.name) / "history"
        self.image_service.result_dir = result_dir
        session_dir = result_dir / "h-1"
        session_dir.mkdir(parents=True)
        (session_dir / "image.png").write_bytes(b"image")
        manifest = {
            "id": "h-1",
            "prompt": "test",
            "mode": "generate",
            "size": "1024x1024",
            "n": 1,
            "status": "completed",
            "urls": ["/api/results/h-1/image.png"],
            "reference_images": ["/api/results/h-1/references/ref.png"],
            "api_id": "api-1",
            "api_name": "Primary",
            "task_id": "task-1",
            "expires_at": 1780309714,
            "created_at": "2026-01-01T00:00:01Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "last_task_id": "task-2",
            "last_status": "failed",
            "last_error": "provider unavailable",
            "last_attempts": [{"api_id": "api-2", "ok": False}],
            "attempts": [{"api_id": "api-1", "ok": True}],
            "response_meta": {"key": "value"},
            "unrelated": "must not leak",
        }
        (session_dir / "session.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        response = self.client.get("/api/sessions?limit=10")
        data = response.get_json()["data"]
        item = data["items"][0]
        self.assertEqual(
            set(item),
            {
                "id", "prompt", "mode", "size", "n", "status", "urls", "images",
                "reference_images", "api_id", "api_name", "task_id", "expires_at",
                "created_at", "updated_at", "last_task_id", "last_status", "last_error",
            },
        )
        self.assertEqual(item["n"], 1)
        self.assertEqual(item["last_status"], "failed")
        self.assertEqual(
            item["images"],
            [{
                "index": 0,
                "url": "/api/results/h-1/image.png",
                "filename": "image.png",
                "session_id": "h-1",
            }],
        )



if __name__ == "__main__":
    main()
