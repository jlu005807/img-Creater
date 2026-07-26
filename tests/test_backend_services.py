import base64
import io
import json
import tempfile
import time
from pathlib import Path
from unittest import TestCase, main

from PIL import Image
from requests.exceptions import Timeout

from backend.services.config_service import ConfigService
from backend.services.image_service import ImageService
from backend.services.prompt_template_service import DEFAULT_TEMPLATES, PromptTemplateService
from backend.services.task_store import TaskStore


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.headers = dict(headers or {})
        self.content = b""

    def json(self):
        return self._payload


class FakeBinaryResponse(FakeResponse):
    def __init__(self, content=b"PNGDATA", status_code=200, headers=None):
        super().__init__(status_code, payload={}, text="", headers=headers or {"Content-Type": "image/png"})
        self.content = content


class BadJsonResponse(FakeResponse):
    def __init__(self, status_code=200, text="<html>bad gateway</html>", headers=None):
        super().__init__(status_code, payload={}, text=text, headers=headers)

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


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


class TimeAdvancingHttpClient(FakeHttpClient):
    def __init__(self, clock, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock

    def get(self, url, **kwargs):
        self.clock["now"] += 2
        return super().get(url, **kwargs)


class CancellingGetHttpClient(FakeHttpClient):
    def __init__(self, store, task_id_holder, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store
        self.task_id_holder = task_id_holder

    def get(self, url, **kwargs):
        with self.store._lock:
            task_id = self.task_id_holder.get("task_id") or next(iter(self.store._tasks))
        self.store.cancel(task_id)
        return super().get(url, **kwargs)


class CancelAwareHttpClient(FakeHttpClient):
    def __init__(self, store, task_id_holder):
        super().__init__()
        self.store = store
        self.task_id_holder = task_id_holder

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        task_id = self.task_id_holder.get("task_id")
        if not task_id:
            with self.store._lock:
                task_id = next(iter(self.store._tasks))
        self.store.cancel(task_id)
        return FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})


def _png_data_url(size=(4, 4), color=(255, 255, 255, 255)):
    image = Image.new("RGBA", size, color)
    return _image_data_url(image)


def _image_data_url(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _decode_image_data_url(data_url):
    encoded = data_url.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")


def _service(config_service, http_client, **kwargs):
    # run_async=False executes the worker inline for deterministic assertions.
    if "result_dir" not in kwargs and "persist_results" not in kwargs:
        kwargs["persist_results"] = False
    if "store" not in kwargs:
        kwargs["store"] = TaskStore()
    return ImageService(
        config_service=config_service,
        http_client=http_client,
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
                    "base_url": "https://fnuu.net",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": False,
                }
            )

            self.assertEqual(first["base_url"], "https://primary.example.com")
            self.assertEqual(first["api_type"], "auto")  # default
            self.assertEqual(second["api_type"], "async")
            self.assertEqual([item["id"] for item in service.list_configs()], [first["id"], second["id"]])

            service.reorder_configs([second["id"], first["id"]])
            reloaded = ConfigService(config_path=config_path)
            self.assertEqual([item["id"] for item in reloaded.list_configs()], [second["id"], first["id"]])

    def test_accepts_auto_api_type_without_rewriting_to_specific_protocol(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ConfigService(config_path=Path(tmp_dir) / "configs.json")

            created = service.create_config(
                {
                    "name": "Auto",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                }
            )

            self.assertEqual(created["api_type"], "auto")
            updated = service.update_config(created["id"], {"name": "Auto Updated"})
            self.assertEqual(updated["api_type"], "auto")

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


class PromptTemplateServiceTests(TestCase):
    def test_default_templates_include_people_generation_examples_exact_text(self):
        templates = {item["id"]: item for item in DEFAULT_TEMPLATES}

        self.assertIn("seed-portrait-basketball-direct-flash", templates)
        self.assertIn("seed-portrait-korean-soft-mist", templates)
        self.assertIn("seed-portrait-subway-candid", templates)

        self.assertIn("Basketball Court Direct Flash Portrait", templates["seed-portrait-basketball-direct-flash"]["title"])
        self.assertIn(
            "35mm color film photography with harsh direct on-camera flash",
            templates["seed-portrait-basketball-direct-flash"]["text"],
        )
        self.assertIn(
            "body angled sideways with naturally arched back and hips gently pushed back",
            templates["seed-portrait-basketball-direct-flash"]["text"],
        )
        self.assertIn("Source: @BubbleBrain", templates["seed-portrait-basketball-direct-flash"]["text"])

        self.assertIn("Korean Editorial Portrait with Soft Mist", templates["seed-portrait-korean-soft-mist"]["title"])
        self.assertIn(
            "9:16 vertical - editorial portrait, single subject soft black mist filter",
            templates["seed-portrait-korean-soft-mist"]["text"],
        )
        self.assertIn("Source: @BubbleBrain", templates["seed-portrait-korean-soft-mist"]["text"])

        self.assertIn("Subway Candid Photo", templates["seed-portrait-subway-candid"]["title"])
        self.assertEqual(
            templates["seed-portrait-subway-candid"]["text"],
            "Prompt:\n\nA beautiful woman looking at her phone on the subway; a candid photo.\nSource: @AntCaveClub | @underwoodxie96",
        )

    def test_initializes_prompt_template_json_with_people_examples(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = PromptTemplateService(template_path=Path(tmp_dir) / "prompt_templates.json")
            templates = {item["id"]: item for item in service.list_templates()}

            self.assertIn("seed-portrait-basketball-direct-flash", templates)
            self.assertIn("Source: @BubbleBrain", templates["seed-portrait-basketball-direct-flash"]["text"])


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

    def test_openai_generation_extracts_image_url_from_non_json_response_text(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Compat",
                        "base_url": "https://api.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(
                        502,
                        '<html>generated: <a href="https://cdn.example.com/result.png?sig=abc">image</a></html>',
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn.example.com/result.png?sig=abc"])
            self.assertEqual(result["response_meta"]["non_json_url_fallback"], True)

    def test_openai_generation_gateway_timeout_page_with_image_urls_is_not_a_result(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Compat",
                        "base_url": "https://api.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            # A 504 gateway page that happens to embed an asset image must not
            # be mistaken for a completed generation.
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(
                        504,
                        '<html><title>Gateway Timeout</title><img src="https://cdn.gateway.com/logo.png"></html>',
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["urls"], [])

    def test_openai_generation_extracts_extensionless_signed_image_url_from_non_json_response_text(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Compat",
                        "base_url": "https://api.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            image_url = (
                "https://cdn.example.com/download/abc123"
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&response-content-type=image%2Fpng"
            )
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(200, f"<html>done <a href=\"{image_url}\">open</a></html>")
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], [image_url])
            self.assertEqual(result["response_meta"]["non_json_url_fallback"], True)

    def test_openai_generation_does_not_treat_non_image_html_links_as_results(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Compat",
                        "base_url": "https://api.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(
                        502,
                        '<html>error page <a href="https://status.example.com/help">help</a></html>',
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("合法 JSON", result["attempts"][0]["error"])
            details = result["attempts"][0]["details"]
            self.assertIn("https://status.example.com/help", details["text"])
            self.assertIn("https://status.example.com/help", details["text_preview"])
            self.assertIn("Expecting value", details["parse_error"])
            self.assertIn("content_type", details)

    def test_openai_generation_non_json_html_surfaces_gateway_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "aiapi1",
                        "base_url": "https://aiapi1.cc.cd",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "retry_count": 0,
                    }
                ],
            )
            html_page = """
            <!DOCTYPE html>
            <html class="no-js" lang="en-US">
              <head><title>A timeout occurred</title></head>
              <body>Cloudflare gateway page</body>
            </html>
            """
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(
                        524,
                        html_page,
                        headers={
                            "Content-Type": "text/html; charset=UTF-8",
                            "Server": "cloudflare",
                            "CF-RAY": "abc123-HKG",
                        },
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            details = result["attempts"][0]["details"]
            self.assertEqual(details["status_code"], 524)
            self.assertEqual(details["http_status"], 524)
            self.assertEqual(details["content_type"], "text/html; charset=UTF-8")
            self.assertEqual(details["server"], "cloudflare")
            self.assertEqual(details["cf_ray"], "abc123-HKG")
            self.assertEqual(details["html_title"], "A timeout occurred")
            self.assertTrue(details["is_html_response"])
            self.assertTrue(details["cloudflare"])
            self.assertIn("Cloudflare", details["gateway_hint"])

    def test_openai_generation_html_504_stops_without_paid_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "aiapi1",
                        "base_url": "https://aiapi1.cc.cd",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "retry_count": 0,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            html_page = """
            <!DOCTYPE html>
            <html class="no-js" lang="en-US">
              <head><title>Gateway Timeout</title></head>
              <body>Cloudflare gateway timeout</body>
            </html>
            """
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(
                        504,
                        html_page,
                        headers={
                            "Content-Type": "text/html; charset=UTF-8",
                            "Server": "cloudflare",
                            "CF-RAY": "timeout-ray-HKG",
                        },
                    ),
                    FakeResponse(200, {"data": [{"b64_json": "SHOULD_NOT_SUBMIT"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("超时", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://aiapi1.cc.cd/v1/images/generations"])
            details = result["attempts"][0]["details"]
            self.assertEqual(details["http_status"], 504)
            self.assertTrue(details["gateway_timeout"])
            self.assertTrue(details["cloudflare"])
            self.assertEqual(details["html_title"], "Gateway Timeout")

    def test_generation_logs_failed_and_successful_provider_attempts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Broken",
                        "base_url": "https://broken.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(502, "<html>temporary upstream error</html>"),
                    FakeResponse(200, {"data": [{"b64_json": "QUJD"}]}),
                ]
            )
            service = _service(config_service, http_client)

            with self.assertLogs("backend.services.image_service", level="INFO") as logs:
                submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
                result = service.poll_generation_status(task_id=submit["task_id"])

            output = "\n".join(logs.output)
            self.assertEqual(result["status"], "completed")
            self.assertIn("[image] provider failed", output)
            self.assertIn("[image] candidate success", output)
            self.assertIn("[image] task completed", output)

    def test_openai_edit_uses_submitted_marked_image_without_mask(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            marked_image = Image.new("RGBA", (6, 6), (10, 20, 30, 255))
            marked_image.putpixel((5, 5), (108, 114, 120, 255))
            marked = _image_data_url(marked_image)
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="edit corner",
                image=marked,
                size="1024x1024",
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            kwargs = http_client.posts[0][1]
            self.assertIn("image", kwargs["files"])
            self.assertNotIn("mask", kwargs["files"])
            sent_image_bytes = kwargs["files"]["image"][1]
            output = Image.open(io.BytesIO(sent_image_bytes)).convert("RGBA")
            self.assertEqual(output.size, (6, 6))
            self.assertEqual(output.getpixel((0, 0)), (10, 20, 30, 255))
            self.assertEqual(output.getpixel((5, 5)), (108, 114, 120, 255))
            self.assertIn("multiple colored regions", kwargs["data"]["prompt"])
            self.assertIn("edit corner", kwargs["data"]["prompt"])

    def test_openai_edit_sends_clean_source_marked_image_and_references_in_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            source = _png_data_url((4, 4), (10, 20, 30, 255))
            marked = _png_data_url((4, 4), (108, 114, 120, 255))
            reference = _png_data_url((4, 4), (220, 120, 40, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="replace the marked shirt",
                source_image=source,
                marked_image=marked,
                reference_images=[reference],
                size="1024x1024",
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            url, kwargs = http_client.posts[0]
            self.assertEqual(url, "https://api.openai.com/v1/images/edits")
            files = kwargs["files"]
            self.assertEqual([item[0] for item in files], ["image[]", "image[]", "image[]"])
            self.assertEqual([item[1][0] for item in files], ["source.png", "marked.png", "ref0.png"])
            self.assertEqual(_decode_image_data_url(source).getpixel((0, 0)), (10, 20, 30, 255))
            sent_source = Image.open(io.BytesIO(files[0][1][1])).convert("RGBA")
            sent_marked = Image.open(io.BytesIO(files[1][1][1])).convert("RGBA")
            sent_ref = Image.open(io.BytesIO(files[2][1][1])).convert("RGBA")
            self.assertEqual(sent_source.getpixel((0, 0)), (10, 20, 30, 255))
            self.assertEqual(sent_marked.getpixel((0, 0)), (108, 114, 120, 255))
            self.assertEqual(sent_ref.getpixel((0, 0)), (220, 120, 40, 255))
            self.assertNotIn("mask", dict(files))
            prompt = kwargs["data"]["prompt"]
            self.assertIn("Image 1 is the clean source image", prompt)
            self.assertIn("Image 2 is the same source image with colored semi-transparent marks", prompt)
            self.assertIn("Different colors may correspond to different edit instructions", prompt)
            self.assertIn("Images 3 and later are references only", prompt)
            self.assertIn("replace the marked shirt", prompt)

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

    def test_openai_generation_uses_generation_timeout_floor_for_slow_sync_providers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "timeout_seconds": 30,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client, generation_timeout=180)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(http_client.posts[0][1]["timeout"], 180)
            self.assertEqual(submit["max_wait_seconds"], 180)

    def test_openai_generation_timeout_stops_without_next_node_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "timeout_seconds": 30,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "timeout_seconds": 30,
                    },
                ],
            )
            _primary, _backup = created
            http_client = FakeHttpClient(
                post_responses=[Timeout("Read timed out."), FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client, generation_timeout=180)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("请求超时", result["error"])
            self.assertIn("避免重复扣费", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://api.openai.com/v1/images/generations"])
            self.assertEqual([call[1]["timeout"] for call in http_client.posts], [180])
            self.assertEqual(submit["max_wait_seconds"], 180)

    def test_grok_imagine_generation_uses_xai_openai_compatible_shape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "xAI",
                        "base_url": "https://api.x.ai",
                        "api_key": "key-1",
                        "model": "grok-imagine-image-lite",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn.example.com/grok.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="971x1619", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(http_client.posts[0][0], "https://api.x.ai/v1/images/generations")
            body = http_client.posts[0][1]["json"]
            self.assertEqual(body["model"], "grok-imagine-image-lite")
            self.assertEqual(body["aspect_ratio"], "9:16")
            self.assertEqual(body["resolution"], "2k")
            self.assertNotIn("size", body)

    def test_grok_imagine_edit_uses_xai_json_image_url_shape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "xAI",
                        "base_url": "https://api.x.ai",
                        "api_key": "key-1",
                        "model": "grok-imagine-image-lite",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            marked = _png_data_url((4, 4), (108, 114, 120, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn.example.com/grok-edit.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="change the marked area",
                image=marked,
                size="1024x1024",
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(http_client.posts[0][0], "https://api.x.ai/v1/images/edits")
            kwargs = http_client.posts[0][1]
            self.assertNotIn("files", kwargs)
            body = kwargs["json"]
            self.assertEqual(body["model"], "grok-imagine-image-lite")
            self.assertIn("multiple colored regions", body["prompt"])
            self.assertIn("change the marked area", body["prompt"])
            self.assertEqual(body["n"], 1)
            self.assertEqual(body["image"]["url"], marked)
            sent = _decode_image_data_url(body["image"]["url"])
            self.assertEqual(sent.getpixel((0, 0)), (108, 114, 120, 255))
            self.assertNotIn("mask", body)
            self.assertNotIn("size", body)

    def test_grok_imagine_reference_generation_uses_xai_json_images_shape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "xAI",
                        "base_url": "https://api.x.ai",
                        "api_key": "key-1",
                        "model": "grok-imagine-image-lite",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            references = [_png_data_url((4, 4)), _png_data_url((6, 6))]
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn.example.com/grok-ref.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(
                prompt="blend these",
                size="1024x1024",
                n=1,
                reference_images=references,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(http_client.posts[0][0], "https://api.x.ai/v1/images/edits")
            kwargs = http_client.posts[0][1]
            self.assertNotIn("files", kwargs)
            body = kwargs["json"]
            self.assertEqual(body["model"], "grok-imagine-image-lite")
            self.assertEqual(body["prompt"], "blend these")
            self.assertEqual(body["images"], [{"type": "image_url", "url": ref} for ref in references])
            self.assertNotIn("size", body)

    def test_grok_imagine_generation_maps_two_to_one_custom_size(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "xAI",
                        "base_url": "https://api.x.ai",
                        "api_key": "key-1",
                        "model": "grok-imagine-image-lite",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn.example.com/grok-wide.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="wide scene", size="2048x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            body = http_client.posts[0][1]["json"]
            self.assertEqual(body["aspect_ratio"], "2:1")
            self.assertEqual(body["resolution"], "2k")

    def test_openai_generation_tracks_client_and_upstream_request_ids(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        200,
                        {"data": [{"b64_json": "QUJD"}]},
                        headers={"x-request-id": "req-upstream-123"},
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(http_client.posts[0][1]["headers"]["X-Client-Request-Id"], submit["task_id"])
            self.assertEqual(result["request_id"], submit["task_id"])
            self.assertEqual(result["upstream_request_id"], "req-upstream-123")

    def test_openai_generation_exposes_response_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        200,
                        {
                            "created": 1713833628,
                            "background": "opaque",
                            "output_format": "png",
                            "quality": "high",
                            "size": "1024x1024",
                            "usage": {"total_tokens": 123},
                            "data": [{"b64_json": "QUJD"}],
                        },
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(
                result["response_meta"],
                {
                    "created": 1713833628,
                    "background": "opaque",
                    "output_format": "png",
                    "quality": "high",
                    "size": "1024x1024",
                    "usage": {"total_tokens": 123},
                },
            )

    def test_cancelled_task_ignores_late_openai_response(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _ = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://api.openai.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    }
                ],
            )
            store = TaskStore()
            task_id_holder = {}
            http_client = CancelAwareHttpClient(store, task_id_holder)
            service = _service(config_service, http_client, store=store)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            task_id_holder["task_id"] = submit["task_id"]
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["urls"], [])
            self.assertEqual(result["error"], "任务已手动停止")

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
                        "api_type": "openai",
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
                        "api_type": "openai",
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

    def test_generation_falls_back_to_next_node_even_when_first_auto_mode_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://primary.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "auto_mode": False,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(500, {"error": {"message": "primary failed"}}),
                    FakeResponse(200, {"data": [{"url": "https://cdn.example.com/ok.png"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn.example.com/ok.png"])
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://primary.example.com/v1/images/generations",
                    "https://backup.example.com/v1/images/generations",
                ],
            )

    def test_openai_timeout_does_not_retry_or_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Slow OpenAI",
                        "base_url": "https://slow.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "timeout_seconds": 60,
                        "retry_count": 3,
                    },
                    {
                        "name": "Relay",
                        "base_url": "https://relay.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "async",
                        "status": True,
                    },
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    Timeout("read timed out"),
                    FakeResponse(200, {"task_id": "up-1", "status": "queued"}),
                ],
                get_responses=[FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("请求超时", result["error"])
            self.assertIn("避免重复扣费", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://slow.example.com/v1/images/generations"])
            self.assertEqual(http_client.gets, [])

    def test_openai_compat_use_async_images_error_switches_same_node_to_async(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, _created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Relay",
                        "base_url": "https://relay.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        503,
                        {
                            "error": {
                                "message": "please use async endpoint POST /async/images",
                                "code": "use_async_images",
                            }
                        },
                    ),
                    FakeResponse(200, {"task_id": "up-1", "status": "queued"}),
                ],
                get_responses=[FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://relay.example.com/v1/images/generations",
                    "https://relay.example.com/async/images",
                ],
            )

    def test_auto_protocol_switches_to_async_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "configs.json"
            config_service = ConfigService(config_path=config_path)
            created = config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://relay.example.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        503,
                        {
                            "error": {
                                "message": "please use async endpoint POST /async/images",
                                "code": "use_async_images",
                            }
                        },
                    ),
                    FakeResponse(200, {"task_id": "up-1", "status": "queued"}),
                ],
                get_responses=[FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])
            reloaded = ConfigService(config_path=config_path)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["effective_api_type"], "async")
            self.assertEqual(result["attempts"][0]["configured_api_type"], "auto")
            self.assertEqual(result["attempts"][0]["effective_api_type"], "async")
            self.assertEqual(reloaded.get_config(created["id"])["api_type"], "auto")
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://relay.example.com/v1/images/generations",
                    "https://relay.example.com/async/images",
                ],
            )

    def test_auto_protocol_uses_openai_first_for_aiapi_host_and_records_request_url(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "aiapi1",
                        "base_url": "https://aiapi1.cc.cd",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "auto",
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
            self.assertEqual(result["api_id"], created[0]["id"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://aiapi1.cc.cd/v1/images/generations"])
            self.assertEqual(result["effective_api_type"], "openai")
            self.assertEqual(result["request_url"], "https://aiapi1.cc.cd/v1/images/generations")
            self.assertEqual(result["attempts"][0]["configured_api_type"], "auto")
            self.assertEqual(result["attempts"][0]["effective_api_type"], "openai")
            self.assertEqual(result["attempts"][0]["request_url"], "https://aiapi1.cc.cd/v1/images/generations")

    def test_auto_protocol_tries_all_supported_protocols_inside_node_before_next_node(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Auto Node",
                        "base_url": "https://auto.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "auto",
                        "status": True,
                        "retry_count": 0,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            primary, backup = created
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(500, {"error": {"message": "openai images failed"}}),
                    FakeResponse(404, {"error": {"message": "async unavailable"}}),
                    FakeResponse(200, {"data": [{"b64_json": "QUJD"}], "output_format": "png"}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["api_id"], primary["id"])
            self.assertNotEqual(result["api_id"], backup["id"])
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://auto.example.com/v1/images/generations",
                    "https://auto.example.com/async/images",
                    "https://auto.example.com/v1/chat/completions",
                ],
            )
            self.assertFalse(result["attempts"][0]["ok"])
            self.assertEqual(result["attempts"][0]["effective_api_type"], "openai")
            self.assertFalse(result["attempts"][1]["ok"])
            self.assertEqual(result["attempts"][1]["effective_api_type"], "async")
            self.assertTrue(result["attempts"][2]["ok"])
            self.assertEqual(result["attempts"][2]["effective_api_type"], "chat")

    def test_auto_protocol_uses_async_first_for_known_async_host_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "configs.json"
            config_service = ConfigService(config_path=config_path)
            created = config_service.create_config(
                {
                    "name": "fnuu",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])
            reloaded = ConfigService(config_path=config_path)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["effective_api_type"], "async")
            self.assertEqual(result["attempts"][0]["configured_api_type"], "auto")
            self.assertEqual(result["attempts"][0]["effective_api_type"], "async")
            self.assertEqual(reloaded.get_config(created["id"])["api_type"], "auto")
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])

    def test_auto_protocol_falls_back_to_openai_when_async_endpoint_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "fnuu",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                    "retry_count": 0,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(404, {"error": {"message": "not found"}}),
                    FakeResponse(200, {"data": [{"b64_json": "QUJD"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["effective_api_type"], "openai")
            self.assertEqual(
                [call[0] for call in http_client.posts],
                [
                    "https://fnuu.net/async/images",
                    "https://fnuu.net/v1/images/generations",
                ],
            )
            self.assertFalse(result["attempts"][0]["ok"])
            self.assertEqual(result["attempts"][0]["effective_api_type"], "async")
            self.assertTrue(result["attempts"][1]["ok"])
            self.assertEqual(result["attempts"][1]["effective_api_type"], "openai")

    def test_auto_protocol_tries_supported_protocols_inside_node_before_next_node(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Auto Node",
                        "base_url": "https://auto.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "auto",
                        "status": True,
                        "retry_count": 0,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            primary, _backup = created
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(404, {"error": {"message": "not found"}}),
                    FakeResponse(404, {"error": {"message": "not found"}}),
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
                    "https://auto.example.com/v1/images/generations",
                    "https://auto.example.com/async/images",
                    "https://auto.example.com/v1/chat/completions",
                ],
            )
            self.assertEqual(
                [attempt["effective_api_type"] for attempt in result["attempts"]],
                ["openai", "async", "chat"],
            )

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
            self.assertEqual(result["attempts"][0]["api_id"], primary["id"])
            self.assertEqual(result["attempts"][0]["api_name"], "Primary")
            self.assertTrue(result["attempts"][0]["ok"])
            self.assertEqual(result["attempts"][0]["configured_api_type"], "auto")
            self.assertEqual(result["attempts"][0]["effective_api_type"], "openai")

    def test_auto_mode_false_still_allows_next_node_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service, created = self._config_service(
                tmp_dir,
                [
                    {
                        "name": "Primary",
                        "base_url": "https://primary.example.com",
                        "api_key": "key-1",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                        "auto_mode": False,
                    },
                    {
                        "name": "Backup",
                        "base_url": "https://backup.example.com",
                        "api_key": "key-2",
                        "model": "gpt-image-2",
                        "api_type": "openai",
                        "status": True,
                    },
                ],
            )
            primary, backup = created
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
            self.assertEqual(len(http_client.posts), 2)
            self.assertEqual(result["attempts"][0]["api_id"], primary["id"])
            self.assertEqual(result["attempts"][1]["api_id"], backup["id"])
            self.assertTrue(result["attempts"][1]["ok"])

    def test_edit_uses_multipart_marked_image_without_mask(self):
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
            marked = _png_data_url((6, 6), (108, 114, 120, 255)).replace("image/png", "image/jpeg", 1)

            submit = service.submit_edit_generation(
                prompt="replace the masked area",
                image=marked,
                size="1024x1024",
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["operation"], "edit")
            url, kwargs = http_client.posts[0]
            self.assertEqual(url, "https://api.openai.com/v1/images/edits")
            self.assertIn("files", kwargs)
            self.assertIn("image", kwargs["files"])
            self.assertNotIn("mask", kwargs["files"])
            self.assertEqual(kwargs["files"]["image"][2], "image/png")
            sent = Image.open(io.BytesIO(kwargs["files"]["image"][1])).convert("RGBA")
            self.assertEqual(sent.getpixel((0, 0)), (108, 114, 120, 255))
            self.assertEqual(kwargs["data"]["model"], "gpt-image-2")
            self.assertIn("multiple colored regions", kwargs["data"]["prompt"])
            self.assertIn("replace the masked area", kwargs["data"]["prompt"])
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

    def test_reference_images_are_persisted_per_history_entry(self):
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
            reference = _png_data_url((3, 5), (20, 120, 220, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            result_dir = Path(tmp_dir) / "results"
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(
                prompt="blend this",
                size="1024x1024",
                n=1,
                reference_images=[reference],
                history_id="history-refs",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["reference_images"]), 1)
            self.assertTrue(result["reference_images"][0].startswith("/api/results/history-refs/references/"))
            saved_name = result["reference_images"][0].rsplit("/", 1)[1]
            saved_ref = Image.open(result_dir / "history-refs" / "references" / saved_name).convert("RGBA")
            self.assertEqual(saved_ref.size, (3, 5))
            self.assertEqual(saved_ref.getpixel((0, 0)), (20, 120, 220, 255))
            manifest = json.loads((result_dir / "history-refs" / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["reference_images"], result["reference_images"])

    def test_reference_images_are_persisted_when_task_is_submitted_even_if_generation_fails(self):
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
            reference = _png_data_url((3, 5), (20, 120, 220, 255))
            http_client = FakeHttpClient(post_responses=[Timeout("read timed out")])
            result_dir = Path(tmp_dir) / "results"
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(
                prompt="blend this",
                size="1024x1024",
                n=1,
                reference_images=[reference],
                history_id="history-refs",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(submit["reference_images"]), 1)
            self.assertTrue(submit["reference_images"][0].startswith("/api/results/history-refs/references/"))
            saved_name = submit["reference_images"][0].rsplit("/", 1)[1]
            saved_ref = Image.open(result_dir / "history-refs" / "references" / saved_name).convert("RGBA")
            self.assertEqual(saved_ref.size, (3, 5))
            self.assertEqual(saved_ref.getpixel((0, 0)), (20, 120, 220, 255))
            manifest = json.loads((result_dir / "history-refs" / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["reference_images"], submit["reference_images"])
            self.assertEqual(manifest["urls"], [])

    def test_persisted_reference_image_urls_can_be_reused_for_generation(self):
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
            result_dir = Path(tmp_dir) / "results"
            ref_dir = result_dir / "history-refs" / "references"
            ref_dir.mkdir(parents=True)
            Image.new("RGBA", (4, 4), (90, 40, 10, 255)).save(ref_dir / "ref.png")
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(
                prompt="reuse ref",
                size="1024x1024",
                n=1,
                reference_images=["/api/results/history-refs/references/ref.png"],
                history_id="history-refs-2",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            sent_ref = Image.open(io.BytesIO(http_client.posts[0][1]["files"][0][1][1])).convert("RGBA")
            self.assertEqual(sent_ref.getpixel((0, 0)), (90, 40, 10, 255))

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

    def test_custom_provider_inlines_local_reference_urls_as_data_urls(self):
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
            result_dir = Path(tmp_dir) / "history"
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client, result_dir=result_dir, persist_results=True)

            submit = service.submit_generation(
                prompt="use the reference",
                size="1024x1024",
                n=1,
                reference_images=[_png_data_url((4, 4))],
                history_id="custom-refs-1",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            refs = http_client.posts[0][1]["json"]["reference_images"]
            self.assertEqual(len(refs), 1)
            # Persisted /api/results/ URLs are local-only; the outgoing body
            # must carry data URLs upstream can actually read.
            self.assertTrue(refs[0].startswith("data:image/png;base64,"))

    def test_custom_url_is_not_auto_switched_to_async_endpoint(self):
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
                    "retry_count": 0,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        503,
                        {
                            "error": {
                                "message": "please use async endpoint POST /async/images",
                                "code": "use_async_images",
                            }
                        },
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertEqual([call[0] for call in http_client.posts], ["https://my-proxy.example.com/api/v2/img"])

    def test_custom_edit_posts_marked_image_without_mask_fields(self):
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
            marked = _png_data_url((4, 4), (108, 114, 120, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn/pic.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(prompt="edit this area", image=marked, n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            body = http_client.posts[0][1]["json"]
            self.assertEqual(body["image"], marked)
            sent = _decode_image_data_url(body["image"])
            self.assertEqual(sent.getpixel((0, 0)), (108, 114, 120, 255))
            self.assertNotIn("mask", body)
            self.assertNotIn("composite", body)
            self.assertNotIn("selection", body)
            self.assertNotIn("edit_mode", body)
            self.assertIn("multiple colored regions", body["prompt"])
            self.assertIn("edit this area", body["prompt"])

    def test_custom_edit_sends_source_marked_and_references_with_prompt_roles(self):
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
            source = _png_data_url((4, 4), (10, 20, 30, 255))
            marked = _png_data_url((4, 4), (108, 114, 120, 255))
            reference = _png_data_url((4, 4), (220, 120, 40, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn/pic.png"}]})]
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="edit this area",
                source_image=source,
                marked_image=marked,
                reference_images=[reference],
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            body = http_client.posts[0][1]["json"]
            self.assertEqual(body["source_image"], source)
            self.assertEqual(body["marked_image"], marked)
            self.assertEqual(body["image"], marked)
            self.assertEqual(body["reference_images"], [reference])
            self.assertEqual(body["images"], [source, marked, reference])
            self.assertIn("Image 1 is the clean source image", body["prompt"])
            self.assertIn("Images 3 and later are references only", body["prompt"])


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

    def test_chat_completions_extracts_images_from_choices_message_content(self):
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
                post_responses=[
                    FakeResponse(
                        200,
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Here it is: https://cdn.example.com/out.png done",
                                    }
                                }
                            ]
                        },
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn.example.com/out.png"])

    def test_chat_completions_extracts_images_from_choices_content_parts_and_images(self):
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
                post_responses=[
                    FakeResponse(
                        200,
                        {
                            "choices": [
                                {
                                    "message": {
                                        "content": [
                                            {"type": "text", "text": "result"},
                                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                                        ],
                                        "images": [{"image_url": {"url": "https://cdn.example.com/extra.png"}}],
                                    }
                                }
                            ]
                        },
                    )
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="hi", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["urls"],
                ["data:image/png;base64,QUJD", "https://cdn.example.com/extra.png"],
            )

    def test_chat_completions_inlines_local_reference_urls_as_data_urls(self):
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
            result_dir = Path(tmp_dir) / "history"
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": [{"b64_json": "QUJD"}]})]
            )
            service = _service(config_service, http_client, result_dir=result_dir, persist_results=True)

            submit = service.submit_generation(
                prompt="use the reference",
                size="1024x1024",
                n=1,
                reference_images=[_png_data_url((4, 4))],
                history_id="chat-refs-1",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            content = http_client.posts[0][1]["json"]["messages"][0]["content"]
            image_parts = [part for part in content if part.get("type") == "image_url"]
            self.assertEqual(len(image_parts), 1)
            # The persisted local /api/results/ URL must be inlined as a data
            # URL before leaving the machine — upstream cannot fetch it.
            self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))


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

    def test_persisted_async_results_clear_expires_at(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
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
                    ),
                    FakeBinaryResponse(b"PNGDATA", headers={"Content-Type": "image/png"}),
                ],
            )
            result_dir = Path(tmp_dir) / "results"
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(
                prompt="a red house",
                size="1024x1024",
                n=1,
                history_id="history-1",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["urls"][0].startswith("/api/results/history-1/"))
            self.assertIsNone(result["expires_at"])
            manifest = json.loads((result_dir / "history-1" / "session.json").read_text(encoding="utf-8"))
            self.assertIsNone(manifest["expires_at"])

    def test_async_relay_strips_openai_v1_suffix_before_submit_and_poll(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net/v1",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(http_client.posts[0][0], "https://fnuu.net/async/images")
            self.assertEqual(http_client.gets[0][0], "https://fnuu.net/async/task/up-1")

    def test_fnuu_async_generation_sends_reference_images_as_image_field(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            reference_images = [_png_data_url((4, 4)), _png_data_url((6, 6))]
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "result": "https://cdn.example.com/a.png"})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(
                prompt="blend these",
                size="1024x1024",
                n=1,
                reference_images=reference_images,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(http_client.posts), 1)
            kwargs = http_client.posts[0][1]
            self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
            body = kwargs["json"]
            self.assertEqual(body["image"], reference_images)
            self.assertEqual(body["model"], "gpt-image-2")
            self.assertEqual(body["size"], "1024x1024")
            self.assertNotIn("reference_images", body)

    def test_fnuu_async_generation_sends_public_reference_url_as_json_image(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            reference_url = "https://assets.example.com/ref.png"
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-url", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(
                prompt="use this public reference",
                size="1024x1024",
                n=1,
                reference_images=[reference_url],
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            kwargs = http_client.posts[0][1]
            self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
            self.assertNotIn("files", kwargs)
            self.assertEqual(kwargs["json"]["image"], reference_url)
            self.assertEqual(http_client.gets[0][0], "https://fnuu.net/async/task/up-url")

    def test_fnuu_async_generation_uploads_single_local_reference_as_multipart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            local_ref = Path(tmp_dir) / "ref.png"
            Image.new("RGBA", (3, 3), (80, 90, 100, 255)).save(local_ref)
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-file", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(
                prompt="use this local reference",
                size="1024x1024",
                n=1,
                reference_images=[str(local_ref)],
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            kwargs = http_client.posts[0][1]
            self.assertNotIn("json", kwargs)
            self.assertNotIn("Content-Type", kwargs["headers"])
            self.assertEqual(kwargs["data"]["model"], "gpt-image-2")
            self.assertEqual(kwargs["data"]["prompt"], "use this local reference")
            filename, content, mime = kwargs["files"]["image"]
            self.assertEqual(filename, "ref.png")
            self.assertEqual(mime, "image/png")
            self.assertTrue(content.startswith(b"\x89PNG"))

    def test_async_relay_edit_sends_backend_marked_image_without_mask_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            marked = _png_data_url((8, 8), (108, 114, 120, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "result": {"url": "https://cdn.example.com/a.png"}})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="edit region",
                image=marked,
                size="1024x1024",
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(http_client.posts), 1)
            kwargs = http_client.posts[0][1]
            self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
            body = kwargs["json"]
            self.assertEqual(body["image"], marked)
            sent = _decode_image_data_url(body["image"])
            self.assertEqual(sent.getpixel((0, 0)), (108, 114, 120, 255))
            self.assertEqual(http_client.gets[0][0], "https://fnuu.net/async/task/up-1")
            self.assertNotIn("mask", body)
            self.assertNotIn("marked_image", body)
            self.assertNotIn("source_image", body)
            self.assertNotIn("reference_images", body)
            self.assertNotIn("images", body)
            self.assertNotIn("composite", body)
            self.assertNotIn("selection", body)
            self.assertNotIn("edit_mode", body)
            self.assertIn("colored semi-transparent marked regions", body["prompt"])
            self.assertIn("edit region", body["prompt"])

    def test_async_relay_edit_sends_source_marked_and_reference_images_with_prompt_roles(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            source = _png_data_url((4, 4), (10, 20, 30, 255))
            marked = _png_data_url((4, 4), (108, 114, 120, 255))
            reference = _png_data_url((4, 4), (220, 120, 40, 255))
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_edit_generation(
                prompt="edit region",
                source_image=source,
                marked_image=marked,
                reference_images=[reference],
                size="1024x1024",
                n=1,
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            body = http_client.posts[0][1]["json"]
            self.assertEqual(body["image"], [source, marked, reference])
            self.assertNotIn("source_image", body)
            self.assertNotIn("marked_image", body)
            self.assertNotIn("reference_images", body)
            self.assertNotIn("images", body)
            self.assertNotIn("mask", body)
            prompt = body["prompt"]
            self.assertIn("Image 1 is the clean source image", prompt)
            self.assertIn("Image 2 is the same source image with colored semi-transparent marks", prompt)
            self.assertIn("Different colors may correspond to different edit instructions", prompt)
            self.assertIn("Images 3 and later are references only", prompt)
            self.assertIn("edit region", prompt)

    def test_async_protocol_is_matched_case_and_whitespace_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://relay.example.com/",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            original = config_service.get_enabled_configs
            config_service.get_enabled_configs = lambda: [{**item, "api_type": " async "} for item in original()]
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(http_client.posts[0][0], "https://relay.example.com/async/images")

    def test_async_relay_submit_timeout_stops_without_paid_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                    "retry_count": 0,
                }
            )
            config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    Timeout("submit timed out"),
                    FakeResponse(200, {"data": [{"url": "https://cdn.example.com/should-not-use.png"}]}),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("提交任务超时", result["error"])
            self.assertIn("避免重复扣费", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])
            self.assertEqual(http_client.gets, [])
            self.assertEqual(result["attempts"][0]["effective_api_type"], "async")

    def test_async_relay_submit_errors_are_not_retried_even_when_retry_count_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                    "retry_count": 3,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    BadJsonResponse(502, "<html>temporary upstream error</html>"),
                    BadJsonResponse(502, "<html>should-not-post-again</html>"),
                ]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])
            self.assertEqual(http_client.gets, [])

    def test_async_relay_keeps_polling_after_upstream_task_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://relay.example.com/",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                    "timeout_seconds": 1,
                }
            )
            clock = {"now": 1000.0}
            http_client = TimeAdvancingHttpClient(
                clock,
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    FakeResponse(200, {"status": "processing"}),
                    FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]}),
                ],
            )
            service = _service(config_service, http_client, async_max_wait=0)

            original_monotonic = time.monotonic
            try:
                time.monotonic = lambda: clock["now"]
                submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
                result = service.poll_generation_status(task_id=submit["task_id"])
            finally:
                time.monotonic = original_monotonic

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn.example.com/a.png"])
            self.assertEqual(len(http_client.gets), 2)
            self.assertEqual(result["upstream_task_id"], "up-1")

    def test_async_relay_uses_returned_poll_url_and_posts_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://relay.example.com/api",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        200,
                        {
                            "task_id": "up-1",
                            "status": "queued",
                            "poll_url": "/async/images/up-1",
                        },
                    )
                ],
                get_responses=[
                    FakeResponse(200, {"status": "processing"}),
                    FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]}),
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(http_client.posts), 1)
            self.assertEqual(http_client.posts[0][0], "https://relay.example.com/api/async/images")
            self.assertEqual(
                [call[0] for call in http_client.gets],
                [
                    "https://relay.example.com/async/images/up-1",
                    "https://relay.example.com/async/images/up-1",
                ],
            )
            self.assertEqual(result["poll_count"], 2)
            self.assertEqual(result["last_poll_status"], "completed")

    def test_fnuu_async_poll_404_switches_to_returned_poll_url_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                    "retry_count": 3,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(
                        200,
                        {
                            "task_id": "up-1",
                            "status": "queued",
                            "poll_url": "/async/images/up-1",
                        },
                    )
                ],
                get_responses=[
                    FakeResponse(404, {"error": {"message": "not found"}}),
                    FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/a.png"]}),
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])
            self.assertEqual(
                [call[0] for call in http_client.gets],
                ["https://fnuu.net/async/task/up-1", "https://fnuu.net/async/images/up-1"],
            )
            self.assertEqual(result["poll_url"], "https://fnuu.net/async/images/up-1")
            self.assertEqual(result["poll_count"], 2)

    def test_async_relay_unwraps_data_object_status_payloads(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://relay.example.com",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"data": {"task_id": "up-1", "status": "queued"}})],
                get_responses=[
                    FakeResponse(
                        200,
                        {"data": {"status": "completed", "urls": ["https://cdn.example.com/a.png"]}},
                    )
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["urls"], ["https://cdn.example.com/a.png"])

    def test_async_relay_keeps_polling_after_poll_timeout_once_task_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    Timeout("Read timed out."),
                    FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]}),
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(http_client.posts), 1)
            self.assertEqual(len(http_client.gets), 2)
            self.assertEqual(result["poll_count"], 2)
            self.assertEqual(result["last_poll_status"], "completed")
            self.assertIsNone(result["last_poll_error"])

    def test_async_relay_keeps_polling_after_poll_returns_non_json_once_task_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    BadJsonResponse(502, "<html>temporary gateway page</html>"),
                    FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/a.png"]}),
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(http_client.posts), 1)
            self.assertEqual(len(http_client.gets), 2)
            self.assertEqual(result["poll_count"], 2)
            self.assertEqual(result["last_poll_status"], "completed")

    def test_async_relay_submit_non_json_surfaces_readable_error_details(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                    "retry_count": 0,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[BadJsonResponse(502, "<html>upstream error</html>")]
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("合法 JSON", result["attempts"][0]["error"])
            self.assertIn("<html>upstream error</html>", result["attempts"][0]["details"]["text"])

    def test_async_relay_completed_without_urls_fails_without_trying_paid_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                    "retry_count": 0,
                }
            )
            config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "completed", "urls": []})],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("未返回图片", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])

    def test_async_relay_failed_status_surfaces_error_message_and_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                    "retry_count": 0,
                }
            )
            config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    FakeResponse(
                        200,
                        {
                            "status": "failed",
                            "error": {"message": "content policy refused this image", "code": "content_policy"},
                        },
                    )
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("content policy refused this image", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])
            self.assertEqual(result["attempts"][0]["effective_api_type"], "async")
            self.assertIn("content_policy", json.dumps(result["attempts"][0]["details"], ensure_ascii=False))

    def test_async_relay_failed_status_on_non_2xx_poll_surfaces_error_without_retrying_paid_submit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                    "retry_count": 0,
                }
            )
            config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    FakeResponse(
                        500,
                        {
                            "status": "failed",
                            "error": {"message": "model refused this prompt", "code": "safety_refusal"},
                        },
                    ),
                    FakeResponse(200, {"status": "completed", "urls": ["https://cdn.example.com/should-not-use.png"]}),
                ],
            )
            service = _service(config_service, http_client)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("model refused this prompt", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])
            self.assertEqual(len(http_client.gets), 1)
            self.assertIn("safety_refusal", json.dumps(result["attempts"][0]["details"], ensure_ascii=False))

    def test_async_relay_completed_urls_are_downloaded_to_session_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                    "retry_count": 0,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[
                    FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/fnuu.png"]}),
                    FakeBinaryResponse(b"PNGDATA", headers={"Content-Type": "image/png"}),
                ],
            )
            result_dir = Path(tmp_dir) / "history"
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(
                prompt="a red house",
                size="1024x1024",
                n=1,
                history_id="session-fnuu",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["urls"][0].startswith("/api/results/session-fnuu/"))
            saved_name = result["urls"][0].rsplit("/", 1)[1]
            self.assertEqual((result_dir / "session-fnuu" / saved_name).read_bytes(), b"PNGDATA")
            self.assertEqual(
                [call[0] for call in http_client.gets],
                ["https://fnuu.net/async/task/up-1", "https://cdn.example.com/fnuu.png"],
            )

    def test_async_relay_result_download_failure_fails_without_paid_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "async",
                    "status": True,
                    "retry_count": 0,
                }
            )
            config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                }
            )
            http_client = FakeHttpClient(
                post_responses=[
                    FakeResponse(200, {"task_id": "up-1", "status": "queued"}),
                    FakeResponse(200, {"data": [{"url": "https://cdn.example.com/should-not-use.png"}]}),
                ],
                get_responses=[
                    FakeResponse(200, {"status": "completed", "result": ["https://cdn.example.com/fnuu.png"]}),
                    FakeResponse(500, {"error": {"message": "expired"}}),
                ],
            )
            result_dir = Path(tmp_dir) / "history"
            service = _service(config_service, http_client, result_dir=result_dir)

            submit = service.submit_generation(
                prompt="a red house",
                size="1024x1024",
                n=1,
                history_id="session-fnuu",
            )
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "failed")
            self.assertIn("结果下载失败", result["error"])
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])
            self.assertEqual(
                [call[0] for call in http_client.gets],
                ["https://fnuu.net/async/task/up-1", "https://cdn.example.com/fnuu.png"],
            )
            manifest = json.loads((result_dir / "session-fnuu" / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["urls"], [])
            self.assertIn("结果下载失败", manifest["response_meta"]["error"])

    def test_cancelled_async_task_does_not_fallback_to_another_protocol_or_node(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_service = ConfigService(config_path=Path(tmp_dir) / "configs.json")
            config_service.create_config(
                {
                    "name": "Relay",
                    "base_url": "https://fnuu.net",
                    "api_key": "key-1",
                    "model": "gpt-image-2",
                    "api_type": "auto",
                    "status": True,
                    "retry_count": 0,
                }
            )
            config_service.create_config(
                {
                    "name": "Backup",
                    "base_url": "https://backup.example.com",
                    "api_key": "key-2",
                    "model": "gpt-image-2",
                    "api_type": "openai",
                    "status": True,
                }
            )
            store = TaskStore()
            holder = {}
            http_client = CancellingGetHttpClient(
                store,
                holder,
                post_responses=[FakeResponse(200, {"task_id": "up-1", "status": "queued"})],
                get_responses=[FakeResponse(200, {"status": "processing"})],
            )
            service = _service(config_service, http_client, store=store)

            submit = service.submit_generation(prompt="a red house", size="1024x1024", n=1)
            holder["task_id"] = submit["task_id"]
            result = service.poll_generation_status(task_id=submit["task_id"])

            self.assertEqual(result["status"], "cancelled")
            self.assertEqual([call[0] for call in http_client.posts], ["https://fnuu.net/async/images"])


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
