from __future__ import annotations

import base64
import io
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from math import gcd
from urllib.parse import urlparse

from .config_service import ConfigService, DEFAULT_MODEL
from .task_store import TaskStore, task_store as default_task_store


_VALID_QUALITY = {"auto", "low", "medium", "high"}
DEFAULT_RESULT_DIR = Path(__file__).resolve().parents[2] / "history"
KNOWN_ASYNC_RELAY_HOSTS = {"fnuu.net", "www.fnuu.net"}
XAI_IMAGE_HOSTS = {"api.x.ai"}


class ImageServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "details": self.details}


class GenerationValidationError(ImageServiceError, ValueError):
    pass


class ProviderRequestError(ImageServiceError):
    pass


class ProviderTimeoutError(ProviderRequestError):
    pass


class UseAsyncImagesRequired(ProviderRequestError):
    pass


class UpstreamTaskFailed(ProviderRequestError):
    pass


class TaskCancelled(Exception):
    pass


class AllProvidersFailed(ImageServiceError):
    pass


class ImageService:
    """Submits image tasks to the configured providers.

    GPT-Image-2 is reached through the standard OpenAI-compatible Images API
    (``POST {base}/v1/images/generations`` and ``POST {base}/v1/images/edits``).
    Nodes default to ``api_type == "auto"``, which means "try supported
    protocols inside this node" (OpenAI Images, async relay, then Chat
    Completions by default) without rewriting the saved node config. Provider
    fallback is separate: after one node exhausts its configured protocol
    candidates, the worker moves to the next enabled node.

    Because OpenAI image calls are slow (often minutes) and can exceed the
    browser's request timeout, the whole lifecycle runs in a worker thread and
    results are stored in an in-process task store. The frontend keeps polling
    ``GET /api/status?task_id=...``; provider fallback happens inside the worker
    so it works uniformly for both protocols.
    """

    def __init__(
        self,
        config_service: ConfigService | None = None,
        http_client: Any | None = None,
        request_timeout: int = 30,
        generation_timeout: int = 180,
        async_poll_interval: float = 3.0,
        async_max_wait: int = 300,
        store: TaskStore | None = None,
        run_async: bool = True,
        result_dir: str | Path | None = DEFAULT_RESULT_DIR,
        persist_results: bool = True,
    ):
        self.config_service = config_service or ConfigService()
        if http_client is None:
            import requests

            http_client = requests.Session()
        self.http_client = http_client
        self.request_timeout = request_timeout
        self.generation_timeout = generation_timeout
        self.async_poll_interval = async_poll_interval
        self.async_max_wait = async_max_wait
        self.store = store or default_task_store
        # When False the worker runs inline (used by tests for determinism).
        self.run_async = run_async
        self.persist_results = persist_results
        self.result_dir = Path(result_dir) if result_dir is not None else DEFAULT_RESULT_DIR

    # ------------------------------------------------------------------ submit

    def submit_generation(
        self,
        prompt: str,
        size: str = "1024x1024",
        n: int = 1,
        quality: str | None = None,
        reference_images: list[str] | None = None,
        history_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": self._normalize_prompt(prompt),
            "size": self._normalize_size(size),
            "n": self._normalize_image_count(n),
        }
        normalized_history_id = self._normalize_history_id(history_id)
        if normalized_history_id:
            payload["history_id"] = normalized_history_id
        quality = self._normalize_quality(quality)
        if quality:
            payload["quality"] = quality
        refs = self._normalize_reference_images(reference_images)
        if refs:
            payload["reference_images"] = refs
        return self._start_task(payload=payload, operation="generate")

    def submit_edit_generation(
        self,
        prompt: str,
        image: str,
        mask: str,
        size: str = "1024x1024",
        n: int = 1,
        edit_mode: str = "mask",
        selection: dict[str, Any] | None = None,
        quality: str | None = None,
        composite: str | None = None,
        history_id: str | None = None,
    ) -> dict[str, Any]:
        edit_mode = str(edit_mode or "mask").strip() or "mask"
        if edit_mode not in {"mask", "selection"}:
            raise GenerationValidationError("edit_mode 只支持 mask 或 selection", status_code=400)
        if selection is not None and not isinstance(selection, dict):
            raise GenerationValidationError("selection 必须是对象", status_code=400)

        payload: dict[str, Any] = {
            "prompt": self._normalize_prompt(prompt),
            "size": self._normalize_size(size),
            "n": self._normalize_image_count(n),
            "image": self._normalize_image_data_url(image, "image"),
            "mask": self._normalize_image_data_url(mask, "mask"),
            "edit_mode": edit_mode,
        }
        normalized_history_id = self._normalize_history_id(history_id)
        if normalized_history_id:
            payload["history_id"] = normalized_history_id
        quality = self._normalize_quality(quality)
        if quality:
            payload["quality"] = quality
        if selection is not None:
            payload["selection"] = selection
        # Pre-composed original+overlay image (frontend auto-generates it); the
        # async/custom relays forward it so an upstream that wants a single
        # blended image can use it directly.
        if composite:
            payload["composite"] = self._normalize_image_data_url(composite, "composite")
        return self._start_task(payload=payload, operation="edit")

    def _start_task(self, payload: dict[str, Any], operation: str) -> dict[str, Any]:
        providers = self.config_service.get_enabled_configs()
        if not providers:
            raise AllProvidersFailed("没有可用的 API 配置，请先启用至少一个节点", status_code=400)

        task_id = self.store.create(operation)
        payload = dict(payload)
        payload["_task_id"] = task_id
        history_id = str(payload.get("history_id") or "").strip() or None
        max_wait_seconds = self._initial_wait_seconds(providers)
        self.store.update(task_id, max_wait_seconds=max_wait_seconds)
        if history_id:
            self.store.update(task_id, history_id=history_id)
        if self.run_async:
            thread = threading.Thread(
                target=self._execute_task,
                args=(task_id, providers, payload, operation),
                daemon=True,
            )
            thread.start()
        else:
            self._execute_task(task_id, providers, payload, operation)
        return {
            "task_id": task_id,
            "status": "queued",
            "operation": operation,
            "history_id": history_id,
            "max_wait_seconds": max_wait_seconds,
        }

    # ------------------------------------------------------------------ worker

    def _execute_task(
        self,
        task_id: str,
        providers: list[dict[str, Any]],
        payload: dict[str, Any],
        operation: str,
    ) -> None:
        history_id = str(payload.get("history_id") or "").strip() or None
        if self.store.is_cancelled(task_id):
            return
        self.store.update(task_id, status="processing", history_id=history_id)
        attempts: list[dict[str, Any]] = []
        # Each provider/mode carries its own positive timeout. Do not cap it
        # with a fixed global generation limit; user-configured timeouts are
        # the source of truth.
        deadline = None
        try:
            for provider in providers:
                if self.store.is_cancelled(task_id):
                    return
                self.store.update(task_id, api_id=provider["id"], api_name=provider["name"])
                ok, urls, expires_at, response_meta, provider_attempts = self._run_provider_plan(
                    provider, payload, operation, deadline
                )
                if self.store.is_cancelled(task_id):
                    return
                attempts.extend(provider_attempts)
                self.store.update(task_id, attempts=list(attempts))
                if not ok:
                    continue

                if not urls:
                    attempts.append(
                        self._failed_attempt(provider, ProviderRequestError("节点返回空图片列表"))
                    )
                    self.store.update(task_id, attempts=list(attempts))
                    continue

                session_id = history_id or task_id
                urls = self._persist_result_urls(provider, session_id, urls)
                self._persist_session_manifest(
                    session_id=session_id,
                    payload=payload,
                    operation=operation,
                    status="completed",
                    urls=urls,
                    provider=provider,
                    task_id=task_id,
                    attempts=list(attempts),
                    expires_at=expires_at,
                    response_meta=response_meta,
                )
                self.store.update(
                    task_id,
                    status="completed",
                    urls=urls,
                    expires_at=expires_at,
                    attempts=list(attempts),
                    error=None,
                    history_id=history_id,
                    response_meta=response_meta,
                    configured_api_type=self._last_attempt_value(attempts, "configured_api_type"),
                    effective_api_type=self._last_attempt_value(attempts, "effective_api_type"),
                )
                return

            self.store.update(
                task_id,
                status="failed",
                attempts=list(attempts),
                error="所有 API 节点均失败",
            )
        except TaskCancelled:
            return
        except UpstreamTaskFailed as exc:
            if not self.store.is_cancelled(task_id):
                attempts.append(self._failed_attempt(getattr(exc, "provider", {}) or {}, exc, getattr(exc, "candidate", None)))
                self.store.update(task_id, status="failed", attempts=list(attempts), error=exc.message)
        except Exception as exc:  # noqa: BLE001 - never let the worker die silently
            if not self.store.is_cancelled(task_id):
                self.store.update(task_id, status="failed", attempts=list(attempts), error=str(exc))

    def _run_provider_plan(
        self,
        provider: dict[str, Any],
        payload: dict[str, Any],
        operation: str,
        deadline: float | None = None,
    ) -> tuple[bool, list[str], Any, dict[str, Any], list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        for candidate in self._provider_mode_candidates(provider):
            self._raise_if_cancelled(payload)
            candidate["request_url"] = self._candidate_request_url(candidate, payload, operation)
            ok, urls, expires_at, response_meta, error = self._run_candidate_with_retries(
                candidate, payload, operation, deadline
            )
            if ok:
                attempts.append(self._successful_attempt(provider, candidate))
                return True, urls, expires_at, response_meta, attempts
            attempts.append(self._failed_attempt(provider, error or ProviderRequestError("provider failed"), candidate))
        return False, [], None, {}, attempts

    def _run_candidate_with_retries(
        self,
        candidate: dict[str, Any],
        payload: dict[str, Any],
        operation: str,
        deadline: float | None = None,
    ) -> tuple[bool, list[str], Any, dict[str, Any], Exception | None]:
        retries = self._provider_retry_count(candidate)
        last_error: Exception | None = None
        for attempt_index in range(retries + 1):
            try:
                self._raise_if_cancelled(payload)
                candidate["request_url"] = self._candidate_request_url(candidate, payload, operation)
                self.store_timeout_hint(payload, candidate)
                urls, expires_at, response_meta = self._run_provider(candidate, payload, operation, deadline)
                self._raise_if_cancelled(payload)
                if not urls:
                    raise ProviderRequestError("provider returned an empty image list")
                return True, urls, expires_at, response_meta, None
            except TaskCancelled:
                raise
            except UpstreamTaskFailed:
                raise
            except UseAsyncImagesRequired as exc:
                last_error = exc
                configured_api_type = str(candidate.get("configured_api_type") or candidate.get("api_type") or "")
                configured_api_type = configured_api_type.strip().lower()
                api_type = str(candidate.get("api_type") or "").strip().lower()
                if api_type in {"auto", "openai"} or configured_api_type in {"auto", "openai"}:
                    try:
                        async_candidate = dict(candidate)
                        async_candidate["configured_api_type"] = str(
                            candidate.get("configured_api_type") or candidate.get("api_type") or "auto"
                        ).strip()
                        async_candidate["api_type"] = "async"
                        async_candidate["effective_api_type"] = "async"
                        async_candidate["request_url"] = self._candidate_request_url(
                            async_candidate, payload, operation
                        )
                        self.store_timeout_hint(payload, async_candidate)
                        urls, expires_at, response_meta = self._run_async_provider(
                            async_candidate, payload, operation, deadline
                        )
                        if urls:
                            candidate["effective_api_type"] = "async"
                            candidate["request_url"] = async_candidate["request_url"]
                            return True, urls, expires_at, response_meta, None
                    except TaskCancelled:
                        raise
                    except UpstreamTaskFailed:
                        raise
                    except Exception as async_exc:  # noqa: BLE001 - record async fallback failure
                        last_error = async_exc
                break
            except Exception as exc:  # noqa: BLE001 - caller records the final failure
                last_error = exc
                if isinstance(exc, ProviderTimeoutError):
                    api_type = str(candidate.get("api_type") or "").strip().lower()
                    message = (
                        "异步中转提交任务超时，可能已被上游接收；已停止自动重试和切换节点以避免重复扣费"
                        if api_type == "async"
                        else "节点请求超时，可能已被上游接收；已停止自动重试和切换节点以避免重复扣费"
                    )
                    timeout_exc = UpstreamTaskFailed(
                        message,
                        status_code=504,
                        details={
                            "api_id": candidate["id"],
                            "api_name": candidate["name"],
                            "request_url": candidate.get("request_url"),
                            "error": str(exc),
                        },
                    )
                    timeout_exc.provider = candidate
                    timeout_exc.candidate = candidate
                    raise timeout_exc
                if attempt_index >= retries:
                    break
        return False, [], None, {}, last_error

    def _provider_mode_candidates(self, provider: dict[str, Any]) -> list[dict[str, Any]]:
        modes = provider.get("modes")
        if provider.get("internal_auto_mode") and isinstance(modes, list) and modes:
            selected_api_type = str(provider.get("api_type") or "openai").strip()
            ordered_modes = [
                mode for mode in modes if str(mode.get("api_type") or selected_api_type).strip() == selected_api_type
            ]
            ordered_modes.extend(
                mode for mode in modes if str(mode.get("api_type") or selected_api_type).strip() != selected_api_type
            )
            candidates = [self._candidate_from_mode(provider, mode, index) for index, mode in enumerate(ordered_modes)]
        else:
            candidates = [self._candidate_from_provider(provider)]
        expanded: list[dict[str, Any]] = []
        for candidate in candidates:
            expanded.extend(self._expand_auto_candidate(candidate))
        return expanded

    def _expand_auto_candidate(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        configured_api_type = str(candidate.get("configured_api_type") or candidate.get("api_type") or "auto")
        configured_api_type = configured_api_type.strip().lower()
        if configured_api_type != "auto":
            return [candidate]
        expanded = []
        for api_type in self._auto_protocol_order(candidate):
            next_candidate = dict(candidate)
            next_candidate["configured_api_type"] = "auto"
            next_candidate["api_type"] = api_type
            next_candidate["effective_api_type"] = api_type
            expanded.append(next_candidate)
        return expanded

    def _auto_protocol_order(self, provider: dict[str, Any]) -> list[str]:
        # Auto means "try the supported protocols inside this node", not
        # "decide whether to try the next node". Custom direct URLs are
        # excluded because they require a full endpoint that cannot be inferred
        # safely from a generic base_url.
        if self._is_known_async_relay(provider.get("base_url")):
            return ["async", "openai", "chat"]
        return ["openai", "async", "chat"]

    def _raise_if_cancelled(self, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("_task_id") or "").strip()
        if task_id and self.store.is_cancelled(task_id):
            raise TaskCancelled()

    @staticmethod
    def _is_known_async_relay(base_url: Any) -> bool:
        host = (urlparse(str(base_url or "")).hostname or "").lower()
        return host in KNOWN_ASYNC_RELAY_HOSTS

    def _candidate_request_url(self, provider: dict[str, Any], payload: dict[str, Any], operation: str) -> str:
        api_type = str(provider.get("api_type") or "openai").strip().lower()
        base_url = str(provider.get("base_url") or "").strip().rstrip("/")
        if api_type == "async":
            return f"{self._async_base_url(base_url)}/async/images"
        if api_type == "custom":
            return base_url
        if api_type == "chat":
            return self._openai_endpoint(base_url, "chat/completions")
        if operation == "edit" or payload.get("reference_images"):
            return self._openai_endpoint(base_url, "images/edits")
        return self._openai_endpoint(base_url, "images/generations")

    @staticmethod
    def _async_base_url(base_url: Any) -> str:
        base = str(base_url or "").strip().rstrip("/")
        if base.lower().endswith("/v1"):
            return base[:-3].rstrip("/")
        return base

    @staticmethod
    def _candidate_from_provider(provider: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(provider)
        configured_api_type = str(candidate.get("api_type") or "auto").strip().lower()
        candidate["configured_api_type"] = configured_api_type
        if configured_api_type == "auto":
            candidate["api_type"] = "openai"
            candidate["effective_api_type"] = "openai"
        else:
            candidate["effective_api_type"] = configured_api_type
        return candidate

    @staticmethod
    def _candidate_from_mode(provider: dict[str, Any], mode: dict[str, Any], index: int) -> dict[str, Any]:
        candidate = dict(provider)
        candidate.update(mode)
        candidate["id"] = provider["id"]
        candidate["name"] = provider["name"]
        candidate["api_key"] = provider["api_key"]
        candidate["mode_name"] = str(mode.get("name") or f"mode-{index + 1}")
        candidate["base_url"] = str(mode.get("base_url") or provider.get("base_url") or "").strip().rstrip("/")
        candidate["model"] = str(mode.get("model") or provider.get("model") or DEFAULT_MODEL).strip()
        configured_api_type = str(mode.get("api_type") or provider.get("api_type") or "auto").strip().lower()
        candidate["configured_api_type"] = configured_api_type
        if configured_api_type == "auto":
            candidate["api_type"] = "openai"
            candidate["effective_api_type"] = "openai"
        else:
            candidate["api_type"] = configured_api_type
            candidate["effective_api_type"] = configured_api_type
        candidate["_internal_mode"] = True
        if "timeout_seconds" not in mode:
            candidate["timeout_seconds"] = provider.get("timeout_seconds")
        if "retry_count" not in mode:
            candidate["retry_count"] = provider.get("retry_count")
        return candidate

    @staticmethod
    def _provider_retry_count(provider: dict[str, Any]) -> int:
        try:
            return max(0, int(provider.get("retry_count", 0)))
        except (TypeError, ValueError):
            return 0

    def _provider_timeout(self, provider: dict[str, Any]) -> float:
        try:
            return max(1.0, float(provider.get("timeout_seconds", self.request_timeout)))
        except (TypeError, ValueError):
            return float(self.request_timeout)

    def _openai_request_timeout(self, provider: dict[str, Any]) -> float:
        return max(self._provider_timeout(provider), float(self.generation_timeout))

    def _initial_wait_seconds(self, providers: list[dict[str, Any]]) -> int | None:
        if not providers:
            return int(self.request_timeout)
        candidates = self._provider_mode_candidates(providers[0])
        provider = candidates[0] if candidates else providers[0]
        wait_seconds = self._client_wait_seconds(provider)
        return None if wait_seconds is None else int(wait_seconds)

    def store_timeout_hint(self, payload: dict[str, Any], provider: dict[str, Any]) -> None:
        task_id = str(payload.get("_task_id") or "").strip()
        if not task_id:
            return
        wait_seconds = self._client_wait_seconds(provider)
        update: dict[str, Any] = {"max_wait_seconds": None if wait_seconds is None else int(wait_seconds)}
        configured_api_type = str(provider.get("configured_api_type") or provider.get("api_type") or "").strip()
        effective_api_type = str(provider.get("effective_api_type") or provider.get("api_type") or "").strip()
        if configured_api_type:
            update["configured_api_type"] = configured_api_type
        if effective_api_type:
            update["effective_api_type"] = effective_api_type
        request_url = str(provider.get("request_url") or "").strip()
        if request_url:
            update["request_url"] = request_url
        self.store.update(task_id, **update)

    def _run_provider(
        self, provider: dict[str, Any], payload: dict[str, Any], operation: str, deadline: float | None = None
    ) -> tuple[list[str], Any, dict[str, Any]]:
        api_type = str(provider.get("api_type") or "openai").strip().lower()
        if api_type == "async":
            return self._run_async_provider(provider, payload, operation, deadline)
        if api_type == "custom":
            return self._run_custom_provider(provider, payload, operation, deadline)
        if api_type == "chat":
            return self._run_chat_completions_provider(provider, payload, operation, deadline)
        return self._run_openai_provider(provider, payload, operation, deadline)

    def _remaining(self, deadline: float | None, request_cap: float | None = None) -> float:
        """Seconds left in the task budget, capped at the active provider
        timeout; never below 1s so a final attempt can fail cleanly."""
        cap = request_cap if request_cap is not None else self.request_timeout
        if deadline is None:
            return float(cap)
        return max(1.0, min(float(cap), deadline - time.monotonic()))

    def _client_wait_seconds(self, provider: dict[str, Any]) -> float | None:
        if str(provider.get("api_type") or "openai").strip().lower() == "async":
            return None
        if str(provider.get("api_type") or "openai").strip().lower() == "openai":
            return self._openai_request_timeout(provider)
        return self._provider_timeout(provider)

    def cancel_generation(self, task_id: str = "") -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise GenerationValidationError("task_id 不能为空", status_code=400)
        if self.store.get(task_id) is None:
            raise GenerationValidationError("任务不存在或已过期", status_code=404)
        self.store.cancel(task_id)
        return self.poll_generation_status(task_id=task_id)

    # --------------------------------------------------- OpenAI-compatible API

    def _run_openai_provider(
        self, provider: dict[str, Any], payload: dict[str, Any], operation: str, deadline: float | None = None
    ) -> tuple[list[str], Any, dict[str, Any]]:
        model = provider.get("model") or DEFAULT_MODEL
        timeout = self._openai_request_timeout(provider)
        task_id = str(payload.get("_task_id") or "").strip()
        if operation == "edit" and self._is_xai_image_provider(provider):
            url = self._openai_endpoint(provider["base_url"], "images/edits")
            body = self._xai_edit_body(provider, payload)
            response = self._http_post(
                url,
                headers=self._auth_headers(provider, json_content=True, request_id=task_id),
                json=body,
                timeout=timeout,
            )
        elif operation == "edit":
            url = self._openai_endpoint(provider["base_url"], "images/edits")
            image_bytes = self._openai_edit_image_png(payload["image"])
            mask_bytes = self._build_openai_mask(payload["image"], payload["mask"], payload.get("selection"))
            files = {
                "image": ("image.png", image_bytes, "image/png"),
                "mask": ("mask.png", mask_bytes, "image/png"),
            }
            data = {
                "model": model,
                "prompt": payload["prompt"],
                "size": payload["size"],
                "n": str(payload["n"]),
            }
            if payload.get("quality"):
                data["quality"] = payload["quality"]
            response = self._http_post(
                url,
                headers=self._auth_headers(provider, request_id=task_id),
                files=files,
                data=data,
                timeout=timeout,
            )
        elif payload.get("reference_images") and self._is_xai_image_provider(provider):
            url = self._openai_endpoint(provider["base_url"], "images/edits")
            body = self._xai_edit_body(provider, payload)
            response = self._http_post(
                url,
                headers=self._auth_headers(provider, json_content=True, request_id=task_id),
                json=body,
                timeout=timeout,
            )
        elif payload.get("reference_images"):
            # gpt-image accepts reference images via the edits endpoint as
            # multiple image[] files (no mask) — used for reference-guided gen.
            url = self._openai_endpoint(provider["base_url"], "images/edits")
            files = []
            for i, ref in enumerate(payload["reference_images"]):
                raw, mime = self._decode_data_url(ref, "参考图")
                files.append(("image[]", (f"ref{i}.{self._ext_for_mime(mime)}", raw, mime)))
            data = {"model": model, "prompt": payload["prompt"], "n": str(payload["n"])}
            if payload["size"] != "auto":
                data["size"] = payload["size"]
            if payload.get("quality"):
                data["quality"] = payload["quality"]
            response = self._http_post(
                url,
                headers=self._auth_headers(provider, request_id=task_id),
                files=files,
                data=data,
                timeout=timeout,
            )
        else:
            url = self._openai_endpoint(provider["base_url"], "images/generations")
            body = {
                "model": model,
                "prompt": payload["prompt"],
                "n": payload["n"],
            }
            # Most OpenAI-compatible endpoints reject size='auto'; omit it and
            # let upstream pick its default in that case.
            if payload["size"] != "auto":
                body["size"] = payload["size"]
            if payload.get("quality"):
                body["quality"] = payload["quality"]
            body = self._adapt_openai_generation_body(provider, body, payload)
            response = self._http_post(
                url,
                headers=self._auth_headers(provider, json_content=True, request_id=task_id),
                json=body,
                timeout=timeout,
            )

        self._record_upstream_request_id(payload, response)
        data = self._parse_response_json(response, provider)
        self._ensure_success_status(response, data, provider)
        return self._extract_openai_images(data, provider), None, self._extract_image_response_meta(data)

    def _xai_edit_body(self, provider: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": provider.get("model") or DEFAULT_MODEL,
            "prompt": payload["prompt"],
            "n": payload["n"],
        }
        refs = payload.get("reference_images") or []
        if refs:
            body["images"] = [{"type": "image_url", "url": ref} for ref in refs]
            return body
        source = payload.get("composite") or payload.get("image")
        body["image"] = {"type": "image_url", "url": source}
        return body

    def _adapt_openai_generation_body(
        self, provider: dict[str, Any], body: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._is_xai_image_provider(provider):
            return body
        adapted = dict(body)
        size = str(payload.get("size") or "").strip().lower()
        if size and size != "auto":
            adapted.pop("size", None)
            adapted["aspect_ratio"] = self._size_to_aspect_ratio(size)
            adapted["resolution"] = self._xai_resolution_for_size(size)
        # xAI image generation docs use OpenAI-compatible auth/endpoint, but
        # the Images body uses aspect_ratio/resolution instead of arbitrary
        # WxH sizes. Keep quality out unless the user reaches xAI through a
        # custom URL that explicitly accepts it.
        adapted.pop("quality", None)
        return adapted

    @staticmethod
    def _is_xai_image_provider(provider: dict[str, Any]) -> bool:
        host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
        model = str(provider.get("model") or "").strip().lower()
        return host in XAI_IMAGE_HOSTS or model.startswith("grok-imagine-image")

    @staticmethod
    def _size_to_aspect_ratio(size: str) -> str:
        try:
            width_text, height_text = size.lower().split("x", 1)
            width = max(1, int(width_text))
            height = max(1, int(height_text))
        except (TypeError, ValueError):
            return "1:1"

        common = gcd(width, height) or 1
        ratio_w = width // common
        ratio_h = height // common
        known = {
            (1, 1): "1:1",
            (1, 2): "1:2",
            (2, 1): "2:1",
            (2, 3): "2:3",
            (3, 2): "3:2",
            (3, 4): "3:4",
            (4, 3): "4:3",
            (9, 16): "9:16",
            (9, 20): "9:20",
            (16, 9): "16:9",
            (20, 9): "20:9",
            (13, 6): "19.5:9",
            (6, 13): "9:19.5",
        }
        if (ratio_w, ratio_h) in known:
            return known[(ratio_w, ratio_h)]

        # Map near-standard custom sizes to the closest supported xAI ratio.
        actual = width / height
        return min(known.values(), key=lambda ratio: abs(ImageService._ratio_value(ratio) - actual))

    @staticmethod
    def _ratio_value(ratio: str) -> float:
        width, height = ratio.split(":", 1)
        return float(width) / float(height)

    @staticmethod
    def _xai_resolution_for_size(size: str) -> str:
        try:
            width_text, height_text = size.lower().split("x", 1)
            long_edge = max(int(width_text), int(height_text))
        except (TypeError, ValueError):
            return "1k"
        if long_edge >= 1400:
            return "2k"
        return "1k"

    @staticmethod
    def _extract_image_response_meta(data: dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        for key in ("created", "background", "output_format", "quality", "size", "usage"):
            if key in data:
                meta[key] = data[key]
        return meta

    def _record_upstream_request_id(self, payload: dict[str, Any], response: Any) -> None:
        task_id = str(payload.get("_task_id") or "").strip()
        if not task_id:
            return
        headers = getattr(response, "headers", {}) or {}
        upstream_request_id = ""
        if hasattr(headers, "get"):
            upstream_request_id = str(headers.get("x-request-id") or headers.get("X-Request-Id") or "").strip()
        self.store.update(task_id, request_id=task_id, upstream_request_id=upstream_request_id or None)

    def _extract_openai_images(self, data: dict[str, Any], provider: dict[str, Any]) -> list[str]:
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise ProviderRequestError(
                "OpenAI 响应缺少图片数据 (data)",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )
        default_format = str(data.get("output_format") or "png").lower()
        urls: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            if b64:
                # Prefer a per-item format if the provider gives one; else fall
                # back to the response-level output_format.
                fmt = str(item.get("output_format") or default_format).lower()
                urls.append(f"data:image/{fmt};base64,{b64}")
                continue
            url = item.get("url")
            if url:
                urls.append(url)
        if not urls:
            raise ProviderRequestError(
                "OpenAI 响应中没有可用的图片 (b64_json/url)",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )
        return urls

    def _build_openai_mask(
        self, image_data_url: str, mask_data_url: str, selection: dict[str, Any] | None
    ) -> bytes:
        """Produce an OpenAI-compatible mask PNG.

        The editor paints the *target* region opaque on a transparent canvas,
        but OpenAI treats *transparent* pixels as the area to edit, so the alpha
        is inverted. When the editor reports the image's letterbox rect inside
        its canvas (``selection.box``) the mask is cropped to it and resized to
        the source image so the edited region lines up pixel-for-pixel.
        """
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ProviderRequestError(
                "局部编辑需要 Pillow 依赖，请先安装 backend/requirements.txt",
                status_code=500,
            ) from exc

        image_bytes, _ = self._decode_data_url(image_data_url, "image")
        mask_bytes, _ = self._decode_data_url(mask_data_url, "mask")
        source = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        width, height = source.size
        mask_img = Image.open(io.BytesIO(mask_bytes)).convert("RGBA")

        box = selection.get("box") if isinstance(selection, dict) else None
        canvas = selection.get("canvas") if isinstance(selection, dict) else None
        canvas_size: tuple[int, int] | None = None
        if isinstance(canvas, dict):
            try:
                canvas_width = int(canvas["width"])
                canvas_height = int(canvas["height"])
                if canvas_width > 0 and canvas_height > 0:
                    canvas_size = (canvas_width, canvas_height)
            except (KeyError, TypeError, ValueError):
                canvas_size = None

        should_crop = mask_img.size != (width, height) and (
            canvas_size is None or mask_img.size == canvas_size
        )
        if should_crop and isinstance(box, dict):
            try:
                x, y = int(box["x"]), int(box["y"])
                w, h = int(box["width"]), int(box["height"])
            except (KeyError, TypeError, ValueError):
                x = y = w = h = 0
            if w > 0 and h > 0:
                mask_img = mask_img.crop((x, y, x + w, y + h))

        if mask_img.size != (width, height):
            mask_img = mask_img.resize((width, height))

        alpha = mask_img.split()[3].point(lambda value: 255 - value)
        out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        out.putalpha(alpha)
        buffer = io.BytesIO()
        out.save(buffer, format="PNG")
        return buffer.getvalue()

    def _openai_edit_image_png(self, image_data_url: str) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ProviderRequestError(
                "局部编辑需要 Pillow 依赖，请先安装 backend/requirements.txt",
                status_code=500,
            ) from exc
        image_bytes, _ = self._decode_data_url(image_data_url, "image")
        source = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _openai_endpoint(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/{path}"
        return f"{base}/v1/{path}"

    # ------------------------------------------------------- async relay (opt)

    def _run_async_provider(
        self, provider: dict[str, Any], payload: dict[str, Any], operation: str, deadline: float | None = None
    ) -> tuple[list[str], Any, dict[str, Any]]:
        base = self._async_base_url(provider["base_url"])
        body: dict[str, Any] = {
            "model": provider.get("model") or DEFAULT_MODEL,
            "prompt": payload["prompt"],
            "n": payload["n"],
        }
        if payload["size"] != "auto":
            body["size"] = payload["size"]
        if payload.get("quality"):
            body["quality"] = payload["quality"]
        if payload.get("reference_images"):
            body["reference_images"] = payload["reference_images"]
        if operation == "edit":
            body["image"] = payload["image"]
            body["mask"] = payload["mask"]
            body["edit_mode"] = payload.get("edit_mode", "mask")
            if payload.get("composite"):
                body["composite"] = payload["composite"]
            if payload.get("selection") is not None:
                body["selection"] = payload["selection"]

        response = self._http_post(
            f"{base}/async/images",
            headers=self._auth_headers(provider, json_content=True),
            json=body,
            timeout=self._provider_timeout(provider),
        )
        data = self._parse_response_json(response, provider)
        self._ensure_success_status(response, data, provider)
        submit_data = self._unwrap_response_data_object(data)
        upstream_task_id = submit_data.get("task_id")
        if not upstream_task_id:
            raise ProviderRequestError(
                "提交任务成功响应缺少 task_id",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )

        task_id = str(payload.get("_task_id") or "").strip()
        poll_url = self._resolve_async_poll_url(base, str(upstream_task_id), submit_data.get("poll_url"))
        if task_id:
            self.store.update(
                task_id,
                upstream_task_id=str(upstream_task_id),
                poll_url=poll_url,
                wait_phase="upstream_processing",
                max_wait_seconds=None,
                poll_count=0,
                last_poll_status=str(submit_data.get("status") or "").strip().lower() or None,
            )

        poll_count = 0
        while True:
            task_id = str(payload.get("_task_id") or "").strip()
            if task_id and self.store.is_cancelled(task_id):
                raise TaskCancelled()
            poll_count += 1
            try:
                status_response = self._http_get(
                    poll_url,
                    headers=self._auth_headers(provider),
                    timeout=self._provider_timeout(provider),
                )
            except ProviderTimeoutError:
                if task_id:
                    self.store.update(task_id, poll_count=poll_count, last_poll_status="timeout")
                time.sleep(self.async_poll_interval)
                continue
            try:
                status_data = self._parse_response_json(status_response, provider)
            except ProviderRequestError as exc:
                if task_id:
                    self.store.update(
                        task_id,
                        poll_count=poll_count,
                        last_poll_status="poll_error",
                        last_poll_error=str(exc),
                    )
                time.sleep(self.async_poll_interval)
                continue
            status_data = self._unwrap_response_data_object(status_data)
            status = str(status_data.get("status", "")).strip().lower()
            if status not in {"completed", "failed"}:
                try:
                    self._ensure_success_status(status_response, status_data, provider)
                except ProviderRequestError as exc:
                    if task_id:
                        self.store.update(
                            task_id,
                            poll_count=poll_count,
                            last_poll_status="poll_error",
                            last_poll_error=str(exc),
                        )
                    time.sleep(self.async_poll_interval)
                    continue
            if task_id:
                self.store.update(
                    task_id,
                    poll_count=poll_count,
                    last_poll_status=status or None,
                    last_poll_error=None,
                )
            if status == "completed":
                urls = status_data.get("urls") if isinstance(status_data.get("urls"), list) else []
                if not urls:
                    exc = UpstreamTaskFailed(
                        "上游任务已完成但未返回图片 URL",
                        status_code=502,
                        details={"api_id": provider["id"], "api_name": provider["name"], "upstream": status_data},
                    )
                    exc.provider = provider
                    exc.candidate = provider
                    raise exc
                return urls, status_data.get("expires_at"), self._extract_image_response_meta(status_data)
            if status == "failed":
                message = self._error_message_from_payload(status_data.get("error") or status_data)
                exc = UpstreamTaskFailed(
                    message or "上游任务失败",
                    status_code=502,
                    details={"api_id": provider["id"], "api_name": provider["name"], "upstream": status_data},
                )
                exc.provider = provider
                exc.candidate = provider
                raise exc
            time.sleep(self.async_poll_interval)

    @staticmethod
    def _unwrap_response_data_object(data: dict[str, Any]) -> dict[str, Any]:
        nested = data.get("data")
        if isinstance(nested, dict):
            return nested
        return data

    @staticmethod
    def _resolve_async_poll_url(base_url: str, upstream_task_id: str, poll_url: Any) -> str:
        raw_poll_url = str(poll_url or "").strip()
        if raw_poll_url.startswith(("http://", "https://")):
            return raw_poll_url
        parsed = urlparse(base_url)
        if raw_poll_url.startswith("/") and parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{raw_poll_url}"
        if raw_poll_url:
            return f"{base_url.rstrip('/')}/{raw_poll_url.lstrip('/')}"
        return f"{base_url.rstrip('/')}/async/images/{upstream_task_id}"

    # ------------------------------------------------------------------ status

    def poll_generation_status(self, api_id: str = "", task_id: str = "") -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise GenerationValidationError("task_id 不能为空", status_code=400)

        task = self.store.get(task_id)
        if task is None:
            raise GenerationValidationError("任务不存在或已过期", status_code=404)

        return {
            "api_id": task.get("api_id"),
            "api_name": task.get("api_name"),
            "history_id": task.get("history_id"),
            "task_id": task_id,
            "operation": task.get("operation"),
            "status": task.get("status"),
            "urls": task.get("urls") or [],
            "attempts": task.get("attempts") or [],
            "expires_at": task.get("expires_at"),
            "max_wait_seconds": task.get("max_wait_seconds"),
            "request_id": task.get("request_id"),
            "request_url": task.get("request_url"),
            "upstream_request_id": task.get("upstream_request_id"),
            "upstream_task_id": task.get("upstream_task_id"),
            "poll_url": task.get("poll_url"),
            "poll_count": task.get("poll_count"),
            "last_poll_status": task.get("last_poll_status"),
            "last_poll_error": task.get("last_poll_error"),
            "wait_phase": task.get("wait_phase"),
            "response_meta": task.get("response_meta") or {},
            "configured_api_type": task.get("configured_api_type"),
            "effective_api_type": task.get("effective_api_type"),
            "error": task.get("error"),
        }

    # ------------------------------------------------------------------ shared

    def _auth_headers(
        self,
        provider: dict[str, Any],
        json_content: bool = False,
        request_id: str | None = None,
    ) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {provider['api_key']}"}
        if json_content:
            headers["Content-Type"] = "application/json"
        if request_id:
            headers["X-Client-Request-Id"] = request_id
        return headers

    def _http_post(self, url: str, **kwargs: Any) -> Any:
        try:
            return self.http_client.post(url, **kwargs)
        except Exception as exc:
            if self._is_timeout_exception(exc):
                raise ProviderTimeoutError(f"请求超时: {exc}", status_code=504) from exc
            raise

    def _http_get(self, url: str, **kwargs: Any) -> Any:
        try:
            return self.http_client.get(url, **kwargs)
        except Exception as exc:
            if self._is_timeout_exception(exc):
                raise ProviderTimeoutError(f"请求超时: {exc}", status_code=504) from exc
            raise

    @staticmethod
    def _is_timeout_exception(exc: Exception) -> bool:
        try:
            import requests

            if isinstance(exc, requests.exceptions.Timeout):
                return True
        except Exception:
            pass
        name = exc.__class__.__name__.lower()
        return "timeout" in name or "timed out" in str(exc).lower()

    def _parse_response_json(self, response: Any, provider: dict[str, Any]) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise ProviderRequestError(
                "API 节点返回的不是合法 JSON",
                status_code=502,
                details={
                    "api_id": provider["id"],
                    "api_name": provider["name"],
                    "status_code": getattr(response, "status_code", None),
                    "text": getattr(response, "text", ""),
                },
            ) from exc
        if not isinstance(data, dict):
            raise ProviderRequestError(
                "API 节点 JSON 响应必须是对象",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )
        return data

    def _ensure_success_status(self, response: Any, data: dict[str, Any], provider: dict[str, Any]) -> None:
        status_code = int(getattr(response, "status_code", 200) or 200)
        if 200 <= status_code < 300:
            return
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type")
        else:
            message = error
        message = message or data.get("message") or getattr(response, "text", "") or "请求失败"
        code = error.get("code") if isinstance(error, dict) else data.get("code")
        if str(code or "").strip() == "use_async_images" or "/async/images" in str(message):
            raise UseAsyncImagesRequired(
                "该节点要求使用异步接口 POST /async/images",
                status_code=502,
                details={
                    "api_id": provider["id"],
                    "api_name": provider["name"],
                    "http_status": status_code,
                    "error": message,
                },
            )
        raise ProviderRequestError(
            f"API 节点请求失败: HTTP {status_code}",
            status_code=502,
            details={
                "api_id": provider["id"],
                "api_name": provider["name"],
                "http_status": status_code,
                "error": message,
            },
        )

    @staticmethod
    def _error_message_from_payload(error: Any) -> str:
        if isinstance(error, dict):
            message = error.get("message") or error.get("error") or error.get("detail") or error.get("reason")
            code = error.get("code") or error.get("type")
            if message and code:
                return f"{message} ({code})"
            if message:
                return str(message)
            if code:
                return str(code)
            return json.dumps(error, ensure_ascii=False)
        if isinstance(error, list):
            return "; ".join(ImageService._error_message_from_payload(item) for item in error if item)
        return str(error or "").strip()

    def _successful_attempt(self, provider: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        attempt = {
            "api_id": provider["id"],
            "api_name": provider["name"],
            "ok": True,
        }
        configured_api_type = str(candidate.get("configured_api_type") or candidate.get("api_type") or "").strip()
        effective_api_type = str(candidate.get("effective_api_type") or candidate.get("api_type") or "").strip()
        if configured_api_type:
            attempt["configured_api_type"] = configured_api_type
        if effective_api_type:
            attempt["effective_api_type"] = effective_api_type or "openai"
        request_url = str(candidate.get("request_url") or "").strip()
        if request_url:
            attempt["request_url"] = request_url
        if not candidate.get("_internal_mode"):
            return attempt
        mode_name = str(candidate.get("mode_name") or "").strip()
        if mode_name:
            attempt["mode_name"] = mode_name
        mode_api_type = str(candidate.get("api_type") or "").strip()
        if mode_api_type:
            attempt["mode_api_type"] = mode_api_type
        return attempt

    def _failed_attempt(
        self,
        provider: dict[str, Any],
        exc: Exception,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details = getattr(exc, "details", {}) if isinstance(exc, ImageServiceError) else {}
        active_candidate = candidate or provider
        attempt = {
            "api_id": provider["id"],
            "api_name": provider["name"],
            "ok": False,
            "error": str(exc),
            "details": details,
        }
        configured_api_type = str(
            active_candidate.get("configured_api_type") or active_candidate.get("api_type") or ""
        ).strip()
        effective_api_type = str(
            active_candidate.get("effective_api_type") or active_candidate.get("api_type") or ""
        ).strip()
        if configured_api_type:
            attempt["configured_api_type"] = configured_api_type
        if effective_api_type:
            attempt["effective_api_type"] = effective_api_type or "openai"
        request_url = str(active_candidate.get("request_url") or "").strip()
        if request_url:
            attempt["request_url"] = request_url
        if not active_candidate.get("_internal_mode"):
            return attempt
        mode_name = str(active_candidate.get("mode_name") or "").strip()
        if mode_name:
            attempt["mode_name"] = mode_name
        mode_api_type = str(active_candidate.get("api_type") or "").strip()
        if mode_api_type:
            attempt["mode_api_type"] = mode_api_type
        return attempt

    @staticmethod
    def _last_attempt_value(attempts: list[dict[str, Any]], key: str) -> Any:
        for attempt in reversed(attempts):
            value = attempt.get(key)
            if value not in (None, ""):
                return value
        return None

    def _persist_result_urls(self, provider: dict[str, Any], history_id: str, urls: list[str]) -> list[str]:
        if not self.persist_results:
            return urls
        persisted: list[str] = []
        for index, url in enumerate(urls):
            persisted.append(self._persist_one_result(provider, history_id, url, index))
        return persisted

    def _persist_one_result(self, provider: dict[str, Any], history_id: str, url: str, index: int) -> str:
        raw: bytes
        mime = "image/png"
        if str(url).startswith("data:image/"):
            raw, mime = self._decode_data_url(url, "result")
        else:
            response = self._http_get(str(url), timeout=self._provider_timeout(provider))
            status_code = int(getattr(response, "status_code", 200) or 200)
            if not 200 <= status_code < 300:
                raise ProviderRequestError(f"结果下载失败: HTTP {status_code}")
            raw = bytes(getattr(response, "content", b"") or b"")
            if not raw:
                raise ProviderRequestError("结果下载失败: 返回内容为空")
            mime = str(getattr(response, "headers", {}).get("Content-Type", "") or mime).split(";", 1)[0]

        target_dir_id = str(history_id or provider["id"])
        target_dir = self.result_dir / target_dir_id
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = self._ext_for_mime(mime)
        filename = f"{int(time.time() * 1000)}-{index}-{uuid.uuid4().hex[:8]}.{ext}"
        (target_dir / filename).write_bytes(raw)
        return f"/api/results/{target_dir_id}/{filename}"

    def _persist_session_manifest(
        self,
        *,
        session_id: str,
        payload: dict[str, Any],
        operation: str,
        status: str,
        urls: list[str],
        provider: dict[str, Any],
        task_id: str,
        attempts: list[dict[str, Any]],
        expires_at: Any,
        response_meta: dict[str, Any] | None = None,
    ) -> None:
        if not self.persist_results:
            return
        now = self._now()
        target_dir = self.result_dir / str(session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = target_dir / "session.json"
        previous: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    previous = loaded
            except Exception:
                previous = {}
        manifest = {
            "id": str(session_id),
            "task_id": task_id,
            "prompt": payload.get("prompt", ""),
            "mode": operation,
            "size": payload.get("size"),
            "n": payload.get("n"),
            "status": status,
            "urls": urls,
            "api_id": provider.get("id"),
            "api_name": provider.get("name"),
            "attempts": attempts,
            "expires_at": expires_at,
            "response_meta": response_meta or {},
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
        }
        tmp_path = manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(manifest_path)

    # ------------------------------------------------------- custom direct URL

    def _run_custom_provider(
        self, provider: dict[str, Any], payload: dict[str, Any], operation: str, deadline: float | None = None
    ) -> tuple[list[str], Any, dict[str, Any]]:
        """POST directly to the exact ``base_url`` the user provided — no
        path construction, no /v1/images prefix, no /async/images relay.
        The response is parsed the same way as the OpenAI-compatible path
        (``data[].b64_json`` / ``data[].url``)."""
        model = provider.get("model") or DEFAULT_MODEL
        timeout = self._remaining(deadline, self._provider_timeout(provider))
        body: dict[str, Any] = {
            "model": model,
            "prompt": payload["prompt"],
            "n": payload["n"],
        }
        if payload["size"] != "auto":
            body["size"] = payload["size"]
        if payload.get("quality"):
            body["quality"] = payload["quality"]
        if payload.get("reference_images"):
            body["reference_images"] = payload["reference_images"]

        # Local edits: send image + mask inline as data URLs (same as async relay).
        if operation == "edit":
            body["image"] = payload["image"]
            body["mask"] = payload["mask"]
            body["edit_mode"] = payload.get("edit_mode", "mask")
            if payload.get("composite"):
                body["composite"] = payload["composite"]
            if payload.get("selection") is not None:
                body["selection"] = payload["selection"]

        url = provider["base_url"].rstrip("/")
        response = self._http_post(
            url,
            headers=self._auth_headers(provider, json_content=True),
            json=body,
            timeout=timeout,
        )
        data = self._parse_response_json(response, provider)
        self._ensure_success_status(response, data, provider)
        return self._extract_openai_images(data, provider), None, self._extract_image_response_meta(data)

    # ------------------------------------------------------- Chat Completions

    def _run_chat_completions_provider(
        self, provider: dict[str, Any], payload: dict[str, Any], operation: str, deadline: float | None = None
    ) -> tuple[list[str], Any, dict[str, Any]]:
        """POST to ``/v1/chat/completions`` — some providers expose gpt-image
        models through this endpoint rather than the Images API.
        Parses ``data``, ``output``, and ``choices[].message.content`` for
        image urls/b64."""
        model = provider.get("model") or DEFAULT_MODEL
        timeout = self._remaining(deadline, self._provider_timeout(provider))
        url = self._openai_endpoint(provider["base_url"], "chat/completions")
        refs = payload.get("reference_images") or []
        if refs:
            content = [{"type": "text", "text": payload["prompt"]}]
            for ref in refs:
                content.append({"type": "image_url", "image_url": {"url": ref}})
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": payload["prompt"]}]
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        response = self._http_post(
            url,
            headers=self._auth_headers(provider, json_content=True),
            json=body,
            timeout=timeout,
        )
        data = self._parse_response_json(response, provider)
        self._ensure_success_status(response, data, provider)
        return self._extract_chat_images(data, provider), None, self._extract_image_response_meta(data)

    def _extract_chat_images(self, data: dict[str, Any], provider: dict[str, Any]) -> list[str]:
        """Extract images from a Chat Completions response: data array (gpt-image),
        output array, or data-URI images embedded in choices text."""
        # 1) data array (gpt-image style)
        items = data.get("data")
        if isinstance(items, list) and items:
            default_fmt = str(data.get("output_format") or "png").lower()
            urls = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("b64_json")
                if b64:
                    fmt = str(item.get("output_format") or default_fmt).lower()
                    urls.append(f"data:image/{fmt};base64,{b64}")
                    continue
                url = item.get("url")
                if url:
                    urls.append(url)
            if urls:
                return urls

        # 2) output array
        output = data.get("output")
        if isinstance(output, list):
            urls = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("b64_json")
                if b64:
                    fmt = str(item.get("output_format") or "png").lower()
                    urls.append(f"data:image/{fmt};base64,{b64}")
                    continue
                url = item.get("url")
                if url:
                    urls.append(url)
            if urls:
                return urls

        # This is genuinely a non-image chat — that's a provider error for an
        # image tool.
        raise ProviderRequestError(
            "Chat Completions 响应中未找到图片数据 (data/output)",
            status_code=502,
            details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
        )

    # --------------------------------------------------------------- normalize

    @staticmethod
    def _normalize_prompt(prompt: str) -> str:
        value = str(prompt or "").strip()
        if not value:
            raise GenerationValidationError("Prompt 不能为空", status_code=400)
        return value

    @staticmethod
    def _normalize_image_count(n: int) -> int:
        try:
            value = int(n)
        except (TypeError, ValueError) as exc:
            raise GenerationValidationError("n 必须是正整数", status_code=400) from exc
        if value < 1:
            raise GenerationValidationError("n 必须大于 0", status_code=400)
        return value

    @staticmethod
    def _normalize_size(size: str) -> str:
        value = str(size or "").strip().lower()
        if value == "auto":
            return "auto"
        parts = value.split("x")
        if len(parts) != 2 or not all(part.isdigit() and int(part) > 0 for part in parts):
            raise GenerationValidationError("size 必须形如 1024x1024 或 auto", status_code=400)
        return value

    @staticmethod
    def _normalize_quality(quality: str | None) -> str | None:
        if quality is None:
            return None
        value = str(quality).strip().lower()
        if not value:
            return None
        if value not in _VALID_QUALITY:
            raise GenerationValidationError("quality 只支持 auto/low/medium/high", status_code=400)
        return value

    @staticmethod
    def _normalize_image_data_url(value: str, field_name: str) -> str:
        text = str(value or "").strip()
        if not text.startswith("data:image/") or ";base64," not in text:
            raise GenerationValidationError(f"{field_name} 必须是 data:image/*;base64 格式", status_code=400)
        return text

    @staticmethod
    def _normalize_reference_images(value: Any) -> list[str]:
        """Validate the optional reference-image list (data URLs). The per-user
        upload cap is enforced on the frontend; here we just keep a sane upper
        bound (8) so a crafted request can't attach an unbounded number."""
        if value is None:
            return []
        if not isinstance(value, list):
            raise GenerationValidationError("reference_images 必须是数组", status_code=400)
        refs: list[str] = []
        for item in value[:8]:
            text = str(item or "").strip()
            if not text:
                continue
            if not text.startswith("data:image/") or ";base64," not in text:
                raise GenerationValidationError("参考图必须是 data:image/*;base64 格式", status_code=400)
            refs.append(text)
        return refs

    @staticmethod
    def _normalize_history_id(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in text)[:120] or None

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _decode_data_url(value: str, field_name: str) -> tuple[bytes, str]:
        text = str(value or "").strip()
        if not text.startswith("data:image/") or ";base64," not in text:
            raise GenerationValidationError(f"{field_name} 必须是 data:image/*;base64 格式", status_code=400)
        header, encoded = text.split(";base64,", 1)
        mime = header[len("data:") :] or "image/png"
        try:
            raw = base64.b64decode(encoded)
        except Exception as exc:
            raise GenerationValidationError(f"{field_name} base64 解码失败", status_code=400) from exc
        return raw, mime

    @staticmethod
    def _ext_for_mime(mime: str) -> str:
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/webp": "webp",
        }.get(mime.lower(), "png")
