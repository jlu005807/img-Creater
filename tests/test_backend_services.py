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


def _service(config_service, http_client, **kwargs):
    # run_async=False executes the worker inline for deterministic assertions.
    if "result_dir" not in kwargs and "persist_results" not in kwargs:
        kwargs["persist_results"] = False
    return ImageService(
        config_service=config_service,
        http_client=http_client,
        store=TaskStore(),
        run_async=False,
        async_poll_interval=0,
        **kwargs,
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

    def test_persists_runtime_options_and_defaults_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ConfigService(config_path=Path(tmp_dir) / "configs.json")

            created = service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://api.openai.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                    "timeout_seconds": 45,
                    "retry_count": 2,
                }
            )
            self.assertTrue(created["auto_mode"])
            self.assertEqual(created["timeout_seconds"], 45)
            self.assertEqual(created["retry_count"], 2)

            updated = service.update_config(created["id"], {"auto_mode": False, "retry_count": 0})
            self.assertFalse(updated["auto_mode"])
            self.assertEqual(updated["retry_count"], 0)

    def test_allows_any_positive_integer_timeout_and_retry_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ConfigService(config_path=Path(tmp_dir) / "configs.json")

            created = service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://api.openai.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                    "timeout_seconds": 9999,
                    "retry_count": 25,
                }
            )

            self.assertEqual(created["timeout_seconds"], 9999)
            self.assertEqual(created["retry_count"], 25)

    def test_persists_internal_modes_and_secret_lookup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ConfigService(config_path=Path(tmp_dir) / "configs.json")

            created = service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://api.openai.com",
                    "api_key": "sk-secret-1234",
                    "model": "gpt-image-2",
                    "status": True,
                    "internal_auto_mode": True,
                    "modes": [
                        {
                            "name": "default",
                            "api_type": "openai",
                            "retry_count": 1,
                        },
                        {
                            "name": "relay",
                            "api_type": "async",
                            "base_url": "https://relay.example.com",
                        },
                    ],
                }
            )

            self.assertTrue(created["internal_auto_mode"])
            self.assertEqual(len(created["modes"]), 2)
            self.assertEqual(created["modes"][0]["retry_count"], 1)
            self.assertEqual(created["modes"][1]["api_type"], "async")
            self.assertEqual(service.get_secret(created["id"])["api_key"], "sk-secret-1234")

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

    def test_generation_omits_size_when_auto(self):
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

            submit = service.submit_generation(prompt="hi", size="auto", n=1)
            service.poll_generation_status(task_id=submit["task_id"])

            # size='auto' must be omitted so non-gpt-image upstreams don't 400.
            self.assertNotIn("size", http_client.posts[0][1]["json"])

    def test_provider_timeout_is_not_capped_by_global_task_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Long",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "status": True,
                        "timeout_seconds": 9999,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client, generation_timeout=2)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(http_client.posts[0][1]["timeout"], 9999)

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

    def test_generation_retries_provider_before_fallback(self):
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
                        "retry_count": 1,
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
            primary, _backup = created
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(500, {"error": {"message": "temporary"}}),
                    FakeResponse(200, {"data": [{"b64_json": "QUJD"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["api_id"], primary["id"])
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://primary.example.com/v1/images/generations",
                    "https://primary.example.com/v1/images/generations",
                ],
            )
            self.assertEqual(result["attempts"], [{"api_id": primary["id"], "api_name": "Primary", "ok": True}])

    def test_auto_mode_false_stops_after_first_failed_node(self):
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
                        "auto_mode": False,
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
            primary, _backup = created
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(500, {"error": {"message": "provider down"}}),
                    FakeResponse(200, {"data": [{"b64_json": "QUJD"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(http_client.posts), 1)
            self.assertEqual(result["attempts"][0]["api_id"], primary["id"])

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

    def test_completed_data_urls_are_persisted_per_history_entry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
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
            provider = created[0]
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            result_dir = Path(tmp_dir) / "results"
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1, history_id="history-1")
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["urls"]), 1)
            self.assertTrue(result["urls"][0].startswith("/api/results/history-1/"))
            saved_name = result["urls"][0].rsplit("/", 1)[1]
            self.assertEqual((result_dir / "history-1" / saved_name).read_bytes(), b"ABC")
            self.assertEqual(result["history_id"], "history-1")
            self.assertEqual(result["api_id"], provider["id"])
            manifest = json.loads((result_dir / "history-1" / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["id"], "history-1")
            self.assertEqual(manifest["prompt"], "a red house")
            self.assertEqual(manifest["size"], "1024x1024")
            self.assertEqual(manifest["mode"], "generate")
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["urls"], result["urls"])

    def test_default_result_dir_is_repo_history_directory(self):
        from backend.services.image_service import DEFAULT_RESULT_DIR

        self.assertEqual(DEFAULT_RESULT_DIR.name, "history")
        self.assertEqual(DEFAULT_RESULT_DIR.parent, Path(__file__).resolve().parents[1])

    def test_internal_mode_auto_fallback_tries_modes_before_next_provider(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            primary = config_service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://primary.example.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                    "retry_count": 0,
                    "internal_auto_mode": True,
                    "modes": [
                        {"name": "default", "api_type": "openai", "retry_count": 0},
                        {"name": "relay", "api_type": "custom", "base_url": "https://fallback.example.com/direct", "retry_count": 0},
                    ],
                }
            )
            backup = config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(500, {"error": {"message": "primary failed"}}),
                    FakeResponse(200, {"data": [{"url": "https://cdn.example.com/ok.png"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["api_id"], primary["id"])
            self.assertEqual(result["urls"], ["https://cdn.example.com/ok.png"])
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://primary.example.com/v1/images/generations",
                    "https://fallback.example.com/direct",
                ],
            )
            self.assertEqual(result["attempts"][0]["api_id"], primary["id"])
            self.assertFalse(result["attempts"][0]["ok"])
            self.assertEqual(result["attempts"][0]["mode_name"], "default")
            self.assertTrue(result["attempts"][1]["ok"])
            self.assertEqual(result["attempts"][1]["mode_name"], "relay")
            self.assertNotEqual(result["api_id"], backup["id"])

    def test_internal_auto_mode_prioritizes_selected_protocol(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://primary.example.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                    "retry_count": 0,
                    "internal_auto_mode": True,
                    "modes": [
                        {
                            "name": "custom-first-in-list",
                            "api_type": "custom",
                            "base_url": "https://custom.example.com/direct",
                            "retry_count": 0,
                        },
                        {"name": "selected-openai", "api_type": "openai", "retry_count": 0},
                    ],
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn.example.com/ok.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(http_client.posts[0][0], "https://primary.example.com/v1/images/generations")
            self.assertEqual(result["attempts"][0]["mode_name"], "selected-openai")


class CustomProviderTests(TestCase):
    def test_custom_posts_directly_to_exact_url(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Direct",
                    "base_url": "https://my-proxy.example.com/api/v2/img",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "custom",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn/pic.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn/pic.png"])
            self.assertEqual(http_client.posts[0][0], "https://my-proxy.example.com/api/v2/img")


class ReferenceImageTests(TestCase):
    def test_openai_generation_with_references_uses_edits_multipart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Primary",
                    "base_url": "https://api.openai.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(
                prompt="blend these",
                size="1024x1024",
                n=1,
                reference_images=[_png_data_url((4, 4)), _png_data_url((4, 4))],
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            url, kwargs = http_client.posts[0]
            self.assertEqual(url, "https://api.openai.com/v1/images/edits")
            # Two reference images sent as image[] multipart files.
            self.assertEqual(len(kwargs["files"]), 2)
            self.assertTrue(all(f[0] == "image[]" for f in kwargs["files"]))


class ChatProviderTests(TestCase):
    def test_chat_completions_posts_and_extracts_images(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Chat",
                    "base_url": "https://api.openai.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "chat",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}], "output_format": "png"})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["data:image/png;base64,QUJD"])
            self.assertEqual(http_client.posts[0][0], "https://api.openai.com/v1/chat/completions")
            body = http_client.posts[0][1]["json"]
            self.assertEqual(body["model"], "gpt-image-2")
            self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])


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
