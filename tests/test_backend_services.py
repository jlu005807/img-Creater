import json
import tempfile
from pathlib import Path
from unittest import TestCase, main

from backend.services.config_service import ConfigService
from backend.services.image_service import ImageService


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class FakeHttpClient:
    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        response = self.post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BackendServiceTests(TestCase):
    def test_config_service_persists_and_reorders_configs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "configs.json"
            service = ConfigService(config_path=config_path)

            first = service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://primary.example.com/",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )
            second = service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "status": False,
                }
            )

            self.assertEqual(first["base_url"], "https://primary.example.com")
            self.assertEqual([item["id"] for item in service.list_configs()], [first["id"], second["id"]])

            service.reorder_configs([second["id"], first["id"]])

            reloaded = ConfigService(config_path=config_path)
            self.assertEqual([item["id"] for item in reloaded.list_configs()], [second["id"], first["id"]])

    def test_submit_generation_falls_back_to_next_enabled_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "configs.json"
            config_service = ConfigService(config_path=config_path)
            primary = config_service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://primary.example.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )
            disabled = config_service.create_config(
                {
                    "name": "Disabled",
                    "base_url": "https://disabled.example.com",
                    "api_key": "key-x",
                    "model": "gpt-image-2",
                    "status": False,
                }
            )
            backup = config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com/",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )
            self.assertFalse(disabled["status"])

            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(status_code=500, payload={"error": "provider down"}),
                    FakeResponse(
                        status_code=200,
                        payload={"task_id": "task-123", "status": "queued", "poll_url": "/poll/task-123"},
                    ),
                ]
            )
            service = ImageService(config_service=config_service, http_client=http_client)

            result = service.submit_generation(prompt="a red house", size="1024x1024", n=1)

            self.assertEqual(result["task_id"], "task-123")
            self.assertEqual(result["api_id"], backup["id"])
            self.assertEqual(result["status"], "queued")
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://primary.example.com/async/images",
                    "https://backup.example.com/async/images",
                ],
            )
            self.assertEqual(
                http_client.posts[1][1]["json"],
                {
                    "model": "gpt-image-2",
                    "prompt": "a red house",
                    "size": "1024x1024",
                    "n": 1,
                },
            )
            self.assertEqual(http_client.posts[1][1]["headers"]["Authorization"], "Bearer key-2")
            self.assertEqual(result["attempts"][0]["api_id"], primary["id"])
            self.assertFalse(result["attempts"][0]["ok"])

    def test_poll_generation_status_uses_api_id_to_query_original_provider(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "configs.json"
            config_service = ConfigService(config_path=config_path)
            provider = config_service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://primary.example.com/",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                get_responses=[
                    FakeResponse(
                        status_code=200,
                        payload={
                            "status": "completed",
                            "urls": ["https://cdn.example.com/image.png"],
                            "expires_at": 1780309714,
                        },
                    )
                ]
            )
            service = ImageService(config_service=config_service, http_client=http_client)

            result = service.poll_generation_status(api_id=provider["id"], task_id="task-123")

            self.assertEqual(
                result,
                {
                    "api_id": provider["id"],
                    "api_name": "Primary",
                    "task_id": "task-123",
                    "status": "completed",
                    "urls": ["https://cdn.example.com/image.png"],
                    "expires_at": 1780309714,
                    "error": None,
                    "raw": {
                        "status": "completed",
                        "urls": ["https://cdn.example.com/image.png"],
                        "expires_at": 1780309714,
                    },
                },
            )
            self.assertEqual(http_client.gets[0][0], "https://primary.example.com/async/images/task-123")
            self.assertEqual(http_client.gets[0][1]["headers"]["Authorization"], "Bearer key-1")


if __name__ == "__main__":
    main()
