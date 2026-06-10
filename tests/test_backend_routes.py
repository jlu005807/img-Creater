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
