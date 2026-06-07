import tempfile
from pathlib import Path
from unittest import TestCase, main

from backend.app import create_app
from backend.services.config_service import ConfigService


class FakeImageService:
    def __init__(self):
        self.submit_calls = []
        self.status_calls = []

    def submit_generation(self, prompt, size="1024x1024", n=1, quality=None, reference_images=None):
        self.submit_calls.append(
            {"prompt": prompt, "size": size, "n": n, "quality": quality, "reference_images": reference_images}
        )
        return {"task_id": "task-123", "status": "queued", "operation": "generate"}

    def submit_edit_generation(
        self,
        prompt,
        image,
        mask,
        size="1024x1024",
        n=1,
        edit_mode="mask",
        selection=None,
        quality=None,
        composite=None,
    ):
        self.submit_calls.append(
            {
                "prompt": prompt,
                "image": image,
                "mask": mask,
                "size": size,
                "n": n,
                "edit_mode": edit_mode,
                "selection": selection,
                "quality": quality,
                "composite": composite,
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

    def test_generation_routes_submit_and_poll_via_image_service(self):
        generate_response = self.client.post(
            "/api/generate",
            json={"prompt": "a red house", "size": "1024x1024", "n": 1},
        )
        self.assertEqual(generate_response.status_code, 202)
        self.assertEqual(generate_response.get_json()["data"]["task_id"], "task-123")
        self.assertEqual(
            self.image_service.submit_calls,
            [{"prompt": "a red house", "size": "1024x1024", "n": 1, "quality": None, "reference_images": None}],
        )

        status_response = self.client.get("/api/status", query_string={"api_id": "api-1", "task_id": "task-123"})
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.get_json()["data"]["status"], "completed")
        self.assertEqual(self.image_service.status_calls, [{"api_id": "api-1", "task_id": "task-123"}])

    def test_edit_route_submits_masked_edit_task(self):
        edit_response = self.client.post(
            "/api/edit",
            json={
                "prompt": "replace selected area",
                "image": "data:image/png;base64,aW1hZ2U=",
                "mask": "data:image/png;base64,bWFzaw==",
                "size": "1024x1024",
                "n": 1,
                "edit_mode": "mask",
                "selection": {"type": "rect", "bbox": {"x": 8, "y": 9, "width": 100, "height": 120}},
            },
        )

        self.assertEqual(edit_response.status_code, 202)
        self.assertEqual(edit_response.get_json()["data"]["task_id"], "edit-task-123")
        self.assertEqual(
            self.image_service.submit_calls[-1],
            {
                "prompt": "replace selected area",
                "image": "data:image/png;base64,aW1hZ2U=",
                "mask": "data:image/png;base64,bWFzaw==",
                "size": "1024x1024",
                "n": 1,
                "edit_mode": "mask",
                "selection": {"type": "rect", "bbox": {"x": 8, "y": 9, "width": 100, "height": 120}},
                "quality": None,
                "composite": None,
            },
        )


if __name__ == "__main__":
    main()
