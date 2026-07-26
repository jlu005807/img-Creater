import tempfile
from pathlib import Path
from unittest import TestCase, main

from backend.app import create_app
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


if __name__ == "__main__":
    main()
