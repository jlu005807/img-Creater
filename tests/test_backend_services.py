import base64
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase, main

from PIL import Image

from backend.services.config_service import ConfigService
from backend.services.image_service import ImageService
from backend.services.task_store import TaskStore


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


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


def _png_data_url(size=(4, 4), color=(255, 255, 255, 255)):
    image = Image.new("RGBA", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _service(config_service, http_client):
    # run_async=False executes the worker inline for deterministic assertions.
    return ImageService(
        config_service=config_service,
        http_client=http_client,
        store=TaskStore(),
        run_async=False,
        async_poll_interval=0,
    )


class ConfigServiceTests(TestCase):
    def test_persists_reorders_and_defaults_api_type(self):
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
                    "name": "Relay",
                    "base_url": "https://relay.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": False,
                }
            )

            self.assertEqual(first["base_url"], "https://primary.example.com")
            self.assertEqual(first["api_type"], "openai")  # default
            self.assertEqual(second["api_type"], "async")
            self.assertEqual([item["id"] for item in service.list_configs()], [first["id"], second["id"]])

            service.reorder_configs([second["id"], first["id"]])
            reloaded = ConfigService(config_path=config_path)
            self.assertEqual([item["id"] for item in reloaded.list_configs()], [second["id"], first["id"]])

    def test_public_config_masks_key_and_blank_update_keeps_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            created = service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://api.openai.com",
                    "api_key": "sk-secret-1234",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )

            public = ConfigService.public_config(created)
            self.assertNotIn("api_key", public)
            self.assertEqual(public["api_key_preview"], "••••1234")
            self.assertTrue(public["has_api_key"])

            # A blank api_key on update keeps the existing key.
            updated = service.update_config(created["id"], {"api_key": "   ", "name": "Renamed"})
            self.assertEqual(updated["api_key"], "sk-secret-1234")
            self.assertEqual(updated["name"], "Renamed")

            # A JSON null api_key must also keep the key, not persist "None".
            updated_null = service.update_config(created["id"], {"api_key": None})
            self.assertEqual(updated_null["api_key"], "sk-secret-1234")


class OpenAIProviderTests(TestCase):
    def _config_service(self, tmp_dir, configs):
        service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
        created = [service.create_config(cfg) for cfg in configs]
        return service, created

    def test_generation_calls_openai_endpoint_and_stores_data_urls(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["data:image/png;base64,QUJD"])
            self.assertEqual(http_client.posts[0][0], "https://api.openai.com/v1/images/generations")
            self.assertEqual(
                http_client.posts[0][1]["json"],
                {"model": "gpt-image-2", "prompt": "a red house", "size": "1024x1024", "n": 1},
            )
            self.assertEqual(http_client.posts[0][1]["headers"]["Authorization"], "Bearer key-1")

    def test_generation_does_not_double_v1_when_base_url_includes_it(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Compat",
                        "base_url": "https://relay.example.com/v1",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(http_client.posts[0][0], "https://relay.example.com/v1/images/generations")

    def test_generation_falls_back_to_next_enabled_node(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://primary.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "status": True,
                    },
                    {
                        "name": "Disabled",
                        "base_url": "https://disabled.example.com",
                        "api_key": "key-x",
                        "model": "gpt-image-2",
                        "status": False,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "status": True,
                    },
                ],
            )
            primary, _disabled, backup = created
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(500, {"error": {"message": "provider down"}}),
                    FakeResponse(200, {"data": [{"b64_json": "QUJD"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["api_id"], backup["id"])
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://primary.example.com/v1/images/generations",
                    "https://backup.example.com/v1/images/generations",
                ],
            )
            self.assertFalse(result["attempts"][0]["ok"])
            self.assertEqual(result["attempts"][0]["api_id"], primary["id"])
            self.assertTrue(result["attempts"][1]["ok"])

    def test_edit_uses_multipart_image_and_mask(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com/",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="replace the masked area",
                image=_png_data_url((6, 6), (10, 20, 30, 255)),
                mask=_png_data_url((6, 6), (255, 255, 255, 255)),
                size="1024x1024",
                n=1,
                edit_mode="mask",
                selection={"type": "brush", "box": {"x": 0, "y": 0, "width": 6, "height": 6}},
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["operation"], "edit")
            url, kwargs = http_client.posts[0]
            self.assertEqual(url, "https://api.openai.com/v1/images/edits")
            self.assertIn("files", kwargs)
            self.assertIn("image", kwargs["files"])
            self.assertIn("mask", kwargs["files"])
            self.assertEqual(kwargs["data"]["model"], "gpt-image-2")
            self.assertEqual(kwargs["data"]["prompt"], "replace the masked area")
            # multipart request must not force a JSON content-type
            self.assertNotIn("Content-Type", kwargs["headers"])


class AsyncRelayProviderTests(TestCase):
    def test_submit_then_poll_upstream_until_completed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            provider = config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://relay.example.com/",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    FakeResponse(
                        200,
                        {"status": "completed", "urls": ["https://cdn.example.com/a.png"], "expires_at": 123},
                    )
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn.example.com/a.png"])
            self.assertEqual(result["expires_at"], 123)
            self.assertEqual(result["api_id"], provider["id"])
            self.assertEqual(http_client.posts[0][0], "https://relay.example.com/async/images")
            self.assertEqual(http_client.gets[0][0], "https://relay.example.com/async/images/up-1")


class StatusLookupTests(TestCase):
    def test_unknown_task_id_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            service = _service(config_service, FakeHttpClient())
            with self.assertRaises(Exception) as ctx:
                service.poll_generation_status(task_id="does-not-exist")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 404)


if __name__ == "__main__":
    main()
