from __future__ import annotations

import base64
import html
import json
import logging
import mimetypes
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from math import gcd
from urllib.parse import unquote, urlparse

from .config_service import ConfigService, DEFAULT_MODEL
from .task_store import TaskStore, task_store as default_task_store


_VALID_QUALITY = {"auto", "low", "medium", "high"}
DEFAULT_RESULT_DIR = Path(__file__).resolve().parents[2] / "history"
KNOWN_ASYNC_RELAY_HOSTS = {"fnuu.net", "www.fnuu.net"}
ASYNC_POLL_FAILURE_LIMIT = 8
FNUU_MAX_IMAGE_BYTES = 12 * 1024 * 1024
XAI_IMAGE_HOSTS = {"api.x.ai"}
EDIT_REFERENCE_PROMPT_INSTRUCTION = (
    "Use the attached images according to their roles. "
    "The marked image may contain multiple colored regions. "
    "Modify only the colored semi-transparent marked regions and preserve the clean source image outside those regions."
)
_IMAGE_URL_RE = re.compile(r"(?:https?://|data:image/)[^\s\"'<>\\)]+", re.IGNORECASE)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
logger = logging.getLogger(__name__)


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
        async_max_wait: int = 900,
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
        self._manifest_locks: dict[str, threading.Lock] = {}
        self._manifest_locks_guard = threading.Lock()

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
        image: str | None = None,
        source_image: str | None = None,
        marked_image: str | None = None,
        reference_images: list[str] | None = None,
        size: str = "1024x1024",
        n: int = 1,
        quality: str | None = None,
        history_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_marked_image = self._normalize_image_data_url(marked_image or image, "marked_image")
        payload: dict[str, Any] = {
            "prompt": self._normalize_prompt(prompt),
            "size": self._normalize_size(size),
            "n": self._normalize_image_count(n),
            # ``image`` is kept as a backward-compatible alias for providers
            # that only know about one edit input.
            "image": normalized_marked_image,
            "marked_image": normalized_marked_image,
        }
        if source_image:
            payload["source_image"] = self._normalize_image_data_url(source_image, "source_image")
        normalized_history_id = self._normalize_history_id(history_id)
        if normalized_history_id:
            payload["history_id"] = normalized_history_id
        quality = self._normalize_quality(quality)
        if quality:
            payload["quality"] = quality
        refs = self._normalize_reference_images(reference_images)
        if refs:
            payload["reference_images"] = refs
        payload["edit_reference_image"] = payload["marked_image"]
        payload["edit_reference_prompt"] = self._edit_reference_prompt(
            payload["prompt"],
            has_source=bool(payload.get("source_image")),
            reference_count=len(payload.get("reference_images") or []),
        )
        return self._start_task(payload=payload, operation="edit")

    def _start_task(self, payload: dict[str, Any], operation: str) -> dict[str, Any]:
        providers = self.config_service.get_enabled_configs()
        if not providers:
            raise AllProvidersFailed("没有可用的 API 配置，请先启用至少一个节点", status_code=400)

        task_id = self.store.create(operation)
        payload = dict(payload)
        payload["_task_id"] = task_id
        history_id = str(payload.get("history_id") or "").strip() or None
        reference_urls: list[str] = []
        if history_id:
            reference_urls = self._persist_reference_images(history_id, payload.get("reference_images") or [])
            if reference_urls:
                payload["reference_images"] = reference_urls
        max_wait_seconds = self._initial_wait_seconds(providers)
        self.store.update(task_id, max_wait_seconds=max_wait_seconds)
        if history_id:
            self.store.update(task_id, history_id=history_id, reference_images=reference_urls)
            self._persist_session_manifest(
                session_id=history_id,
                payload=payload,
                operation=operation,
                status="queued",
                urls=[],
                reference_images=reference_urls,
                provider={},
                task_id=task_id,
                attempts=[],
                expires_at=None,
                response_meta={},
            )
        logger.info(
            "[image] task queued task_id=%s operation=%s history_id=%s providers=%d size=%s n=%s references=%d",
            task_id,
            operation,
            history_id or "",
            len(providers),
            payload.get("size"),
            payload.get("n"),
            len(payload.get("reference_images") or []),
        )
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
            "reference_images": reference_urls,
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
        logger.info("[image] task processing task_id=%s operation=%s history_id=%s", task_id, operation, history_id or "")
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
                logger.info(
                    "[image] provider start task_id=%s operation=%s api_id=%s api_name=%s api_type=%s",
                    task_id,
                    operation,
                    provider.get("id"),
                    provider.get("name"),
                    provider.get("api_type"),
                )
                ok, urls, expires_at, response_meta, provider_attempts = self._run_provider_plan(
                    provider, payload, operation, deadline
                )
                if self.store.is_cancelled(task_id):
                    return
                attempts.extend(provider_attempts)
                self.store.update(task_id, attempts=list(attempts))
                if not ok:
                    last_error = provider_attempts[-1].get("error") if provider_attempts else ""
                    logger.warning(
                        "[image] provider failed task_id=%s api_id=%s api_name=%s attempts=%d error=%s",
                        task_id,
                        provider.get("id"),
                        provider.get("name"),
                        len(provider_attempts),
                        last_error,
                    )
                    continue

                if not urls:
                    attempts.append(
                        self._failed_attempt(provider, ProviderRequestError("节点返回空图片列表"))
                    )
                    self.store.update(task_id, attempts=list(attempts))
                    logger.warning(
                        "[image] provider returned empty urls task_id=%s api_id=%s api_name=%s",
                        task_id,
                        provider.get("id"),
                        provider.get("name"),
                    )
                    continue

                session_id = history_id or task_id
                urls = self._persist_result_urls(provider, session_id, urls)
                if self._all_urls_are_persisted_results(urls):
                    expires_at = None
                reference_urls = self._persist_reference_images(
                    session_id, payload.get("reference_images") or []
                )
                self._persist_session_manifest(
                    session_id=session_id,
                    payload=payload,
                    operation=operation,
                    status="completed",
                    urls=urls,
                    reference_images=reference_urls,
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
                    reference_images=reference_urls,
                    expires_at=expires_at,
                    attempts=list(attempts),
                    error=None,
                    history_id=history_id,
                    response_meta=response_meta,
                    configured_api_type=self._last_attempt_value(attempts, "configured_api_type"),
                    effective_api_type=self._last_attempt_value(attempts, "effective_api_type"),
                )
                logger.info(
                    "[image] task completed task_id=%s operation=%s api_id=%s api_name=%s effective_api_type=%s urls=%d history_id=%s",
                    task_id,
                    operation,
                    provider.get("id"),
                    provider.get("name"),
                    self._last_attempt_value(attempts, "effective_api_type") or provider.get("api_type"),
                    len(urls),
                    session_id,
                )
                return

            logger.warning("[image] task failed task_id=%s operation=%s attempts=%d error=all providers failed", task_id, operation, len(attempts))
            self._persist_failed_session_manifest(
                history_id=history_id,
                payload=payload,
                operation=operation,
                task_id=task_id,
                attempts=list(attempts),
                error="所有 API 节点均失败",
            )
            self.store.update(
                task_id,
                status="failed",
                attempts=list(attempts),
                error="所有 API 节点均失败",
            )
        except TaskCancelled:
            logger.info("[image] task cancelled task_id=%s operation=%s", task_id, operation)
            self._persist_failed_session_manifest(
                history_id=history_id,
                payload=payload,
                operation=operation,
                task_id=task_id,
                attempts=list(attempts),
                error="任务已手动停止",
                status="cancelled",
            )
            return
        except UpstreamTaskFailed as exc:
            if not self.store.is_cancelled(task_id):
                attempts.append(self._failed_attempt(getattr(exc, "provider", {}) or {}, exc, getattr(exc, "candidate", None)))
                self._persist_failed_session_manifest(
                    history_id=history_id,
                    payload=payload,
                    operation=operation,
                    task_id=task_id,
                    attempts=list(attempts),
                    error=exc.message,
                )
                self.store.update(task_id, status="failed", attempts=list(attempts), error=exc.message)
                logger.warning("[image] task failed task_id=%s operation=%s error=%s", task_id, operation, exc.message)
        except Exception as exc:  # noqa: BLE001 - never let the worker die silently
            if not self.store.is_cancelled(task_id):
                self._persist_failed_session_manifest(
                    history_id=history_id,
                    payload=payload,
                    operation=operation,
                    task_id=task_id,
                    attempts=list(attempts),
                    error=str(exc),
                )
                self.store.update(task_id, status="failed", attempts=list(attempts), error=str(exc))
                logger.exception("[image] task crashed task_id=%s operation=%s error=%s", task_id, operation, exc)

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
                task_id = str(payload.get("_task_id") or "").strip()
                logger.info(
                    "[image] candidate request task_id=%s operation=%s api_id=%s api_name=%s configured_api_type=%s effective_api_type=%s attempt=%d/%d url=%s",
                    task_id,
                    operation,
                    candidate.get("id"),
                    candidate.get("name"),
                    candidate.get("configured_api_type") or candidate.get("api_type"),
                    candidate.get("effective_api_type") or candidate.get("api_type"),
                    attempt_index + 1,
                    retries + 1,
                    candidate.get("request_url"),
                )
                urls, expires_at, response_meta = self._run_provider(candidate, payload, operation, deadline)
                self._raise_if_cancelled(payload)
                if not urls:
                    raise ProviderRequestError("provider returned an empty image list")
                logger.info(
                    "[image] candidate success task_id=%s api_id=%s api_name=%s effective_api_type=%s urls=%d",
                    task_id,
                    candidate.get("id"),
                    candidate.get("name"),
                    candidate.get("effective_api_type") or candidate.get("api_type"),
                    len(urls),
                )
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
                        if isinstance(async_exc, ProviderTimeoutError):
                            # 提交可能已被上游接收，禁止再切换节点重复提交
                            self._raise_upstream_timeout(async_candidate, async_exc)
                        last_error = async_exc
                break
            except Exception as exc:  # noqa: BLE001 - caller records the final failure
                last_error = exc
                logger.warning(
                    "[image] candidate failed task_id=%s api_id=%s api_name=%s effective_api_type=%s attempt=%d/%d error=%s",
                    str(payload.get("_task_id") or "").strip(),
                    candidate.get("id"),
                    candidate.get("name"),
                    candidate.get("effective_api_type") or candidate.get("api_type"),
                    attempt_index + 1,
                    retries + 1,
                    exc,
                )
                if isinstance(exc, ProviderTimeoutError):
                    self._raise_upstream_timeout(candidate, exc)
                if attempt_index >= retries:
                    break
        return False, [], None, {}, last_error

    def _raise_upstream_timeout(self, candidate: dict[str, Any], exc: Exception) -> None:
        api_type = str(candidate.get("api_type") or "").strip().lower()
        message = (
            "异步中转提交任务超时，可能已被上游接收；已停止自动重试和切换节点以避免重复扣费"
            if api_type == "async"
            else "节点请求超时，可能已被上游接收；已停止自动重试和切换节点以避免重复扣费"
        )
        timeout_details = {
            "api_id": candidate["id"],
            "api_name": candidate["name"],
            "request_url": candidate.get("request_url"),
            "error": str(exc),
        }
        if isinstance(exc, ImageServiceError) and exc.details:
            timeout_details.update(exc.details)
            timeout_details["provider_error"] = str(exc)
            timeout_details["request_url"] = candidate.get("request_url")
        timeout_exc = UpstreamTaskFailed(
            message,
            status_code=504,
            details=timeout_details,
        )
        timeout_exc.provider = candidate
        timeout_exc.candidate = candidate
        raise timeout_exc

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

    @staticmethod
    def _is_fnuu_provider(provider: dict[str, Any]) -> bool:
        host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
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
        if str(provider.get("api_type") or "").strip().lower() == "async":
            return 0
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
            files = self._openai_edit_files(payload)
            data = {
                "model": model,
                "prompt": payload.get("edit_reference_prompt") or payload["prompt"],
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
                raw, mime, _ = self._reference_image_bytes(ref, "参考图")
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
            "prompt": payload.get("edit_reference_prompt") or payload["prompt"],
            "n": payload["n"],
        }
        refs = (
            self._edit_image_sequence(payload)
            if payload.get("marked_image") or payload.get("edit_reference_image") or payload.get("image")
            else payload.get("reference_images") or []
        )
        if len(refs) > 1:
            body["images"] = [
                {"type": "image_url", "url": self._reference_for_json_image(ref, f"images[{i}]")}
                for i, ref in enumerate(refs)
            ]
        else:
            body["image"] = {"type": "image_url", "url": self._reference_for_json_image(refs[0], "image")}
        return body

    def _openai_edit_files(self, payload: dict[str, Any]) -> Any:
        sequence = self._edit_image_sequence(payload)
        if len(sequence) == 1:
            image_bytes, _mime, _filename = self._reference_image_bytes(sequence[0], "edit_reference_image")
            return {
                "image": ("edit-reference.png", image_bytes, "image/png"),
            }

        files = []
        names = self._edit_image_names(payload)
        for name, data_url in zip(names, sequence):
            raw, mime, _ = self._reference_image_bytes(data_url, name)
            files.append(("image[]", (f"{name}.{self._ext_for_mime(mime)}", raw, mime)))
        return files

    @staticmethod
    def _edit_image_sequence(payload: dict[str, Any]) -> list[str]:
        source = payload.get("source_image")
        marked = payload.get("marked_image") or payload.get("edit_reference_image") or payload.get("image")
        refs = payload.get("reference_images") or []
        if source:
            return [source, marked, *refs]
        return [marked, *refs]

    @staticmethod
    def _edit_image_names(payload: dict[str, Any]) -> list[str]:
        refs = payload.get("reference_images") or []
        if payload.get("source_image"):
            return ["source", "marked", *[f"ref{i}" for i in range(len(refs))]]
        return ["marked", *[f"ref{i}" for i in range(len(refs))]]

    def _add_edit_images_to_json_body(self, body: dict[str, Any], payload: dict[str, Any]) -> None:
        sequence = self._edit_image_sequence(payload)
        marked = self._reference_for_json_image(
            payload.get("marked_image") or payload.get("edit_reference_image") or payload.get("image"),
            "marked_image",
        )
        body["image"] = marked
        body["marked_image"] = marked
        if payload.get("source_image"):
            body["source_image"] = self._reference_for_json_image(payload["source_image"], "source_image")
        refs = payload.get("reference_images") or []
        if refs:
            body["reference_images"] = [
                self._reference_for_json_image(ref, f"reference_images[{index}]")
                for index, ref in enumerate(refs)
            ]
        if len(sequence) > 1:
            body["images"] = [
                self._reference_for_json_image(ref, f"images[{index}]")
                for index, ref in enumerate(sequence)
            ]

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
        for key in ("created", "background", "output_format", "quality", "size", "usage", "non_json_url_fallback"):
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
                details={"api_id": provider["id"], "api_name": provider["name"], "response": self._compact_error_payload(data)},
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
                details={"api_id": provider["id"], "api_name": provider["name"], "response": self._compact_error_payload(data)},
            )
        return urls

    @staticmethod
    def _edit_reference_prompt(prompt: str, has_source: bool = False, reference_count: int = 0) -> str:
        if has_source:
            roles = [
                "Image 1 is the clean source image to edit.",
                "Image 2 is the same source image with colored semi-transparent marks indicating the target edit regions. Different colors may correspond to different edit instructions from the user.",
            ]
            if reference_count:
                roles.append(
                    "Images 3 and later are references only for style, identity, object, material, or composition."
                )
        else:
            roles = [
                "Image 1 is the source image with colored semi-transparent marks indicating the target edit regions. Different colors may correspond to different edit instructions from the user.",
            ]
            if reference_count:
                roles.append(
                    "Images 2 and later are references only for style, identity, object, material, or composition."
                )
        roles.append("Modify only the marked area while preserving unmarked areas as much as possible.")
        return f"{EDIT_REFERENCE_PROMPT_INSTRUCTION}\n{' '.join(roles)}\n\nUser prompt: {prompt}"

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
        is_fnuu = self._is_fnuu_provider(provider)
        if is_fnuu:
            post_kwargs = self._fnuu_async_submit_kwargs(provider, payload, operation)
        else:
            body: dict[str, Any] = {
                "model": provider.get("model") or DEFAULT_MODEL,
                "prompt": payload.get("edit_reference_prompt") if operation == "edit" else payload["prompt"],
                "n": payload["n"],
            }
            if payload["size"] != "auto":
                body["size"] = payload["size"]
            if payload.get("quality"):
                body["quality"] = payload["quality"]
            if payload.get("reference_images") and operation != "edit":
                body["reference_images"] = [
                    self._reference_for_json_image(ref, f"reference_images[{index}]")
                    for index, ref in enumerate(payload["reference_images"])
                ]
            if operation == "edit":
                self._add_edit_images_to_json_body(body, payload)
            post_kwargs = {
                "headers": self._auth_headers(provider, json_content=True),
                "json": body,
                "timeout": self._provider_timeout(provider),
            }

        response = self._http_post(f"{base}/async/images", **post_kwargs)
        # 提交成功后开始计时；超过 async_max_wait 则停止轮询（任务可能仍在上游计费）。
        poll_start = time.monotonic()
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
        returned_poll_url = self._resolve_async_poll_url(base, str(upstream_task_id), submit_data.get("poll_url"))
        if is_fnuu:
            poll_urls = [f"{base.rstrip('/')}/async/task/{upstream_task_id}"]
            for candidate_url in (returned_poll_url, f"{base.rstrip('/')}/async/images/{upstream_task_id}"):
                if candidate_url not in poll_urls:
                    poll_urls.append(candidate_url)
        else:
            poll_urls = [returned_poll_url]
        poll_index = 0
        poll_url = poll_urls[poll_index]
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
        consecutive_poll_failures = 0

        def raise_poll_abort(message: str, last_error: Any) -> None:
            failed = UpstreamTaskFailed(
                message,
                status_code=504,
                details={
                    "api_id": provider["id"],
                    "api_name": provider["name"],
                    "upstream_task_id": str(upstream_task_id),
                    "poll_urls": poll_urls,
                    "last_poll_url": poll_url,
                    "error": str(last_error or ""),
                },
            )
            failed.provider = provider
            failed.candidate = provider
            raise failed

        while True:
            task_id = str(payload.get("_task_id") or "").strip()
            if task_id and self.store.is_cancelled(task_id):
                raise TaskCancelled()
            if time.monotonic() - poll_start > self.async_max_wait:
                raise_poll_abort(
                    f"异步任务等待超过 {self.async_max_wait} 秒仍未完成，已停止轮询；"
                    "上游任务可能仍在计费，请稍后在中转站确认",
                    None,
                )
            poll_count += 1

            def handle_poll_error(exc: ProviderRequestError) -> bool:
                nonlocal poll_index, poll_url
                if self._is_poll_not_found_error(exc):
                    if poll_index + 1 < len(poll_urls):
                        poll_index += 1
                        poll_url = poll_urls[poll_index]
                        if task_id:
                            self.store.update(
                                task_id,
                                poll_count=poll_count,
                                poll_url=poll_url,
                                last_poll_status="poll_url_fallback",
                                last_poll_error=str(exc),
                            )
                        return True
                    failed = UpstreamTaskFailed(
                        f"异步任务轮询 HTTP 404，所有已知轮询地址均找不到任务: {exc}",
                        status_code=502,
                        details={
                            "api_id": provider["id"],
                            "api_name": provider["name"],
                            "poll_urls": poll_urls,
                            "last_poll_url": poll_url,
                            "error": str(exc),
                        },
                    )
                    failed.provider = provider
                    failed.candidate = provider
                    raise failed
                if task_id:
                    self.store.update(
                        task_id,
                        poll_count=poll_count,
                        last_poll_status="poll_error",
                        last_poll_error=str(exc),
                    )
                return False

            try:
                status_response = self._http_get(
                    poll_url,
                    headers=self._auth_headers(provider),
                    timeout=self._provider_timeout(provider),
                )
            except ProviderTimeoutError as exc:
                consecutive_poll_failures += 1
                if consecutive_poll_failures >= ASYNC_POLL_FAILURE_LIMIT:
                    raise_poll_abort(
                        f"异步任务连续 {ASYNC_POLL_FAILURE_LIMIT} 次轮询失败，已停止轮询；"
                        f"上游任务可能仍在计费，请稍后在中转站确认: {exc}",
                        exc,
                    )
                if task_id:
                    self.store.update(task_id, poll_count=poll_count, last_poll_status="timeout")
                time.sleep(self.async_poll_interval)
                continue
            try:
                status_data = self._parse_response_json(status_response, provider)
            except ProviderRequestError as exc:
                if not handle_poll_error(exc):
                    consecutive_poll_failures += 1
                    if consecutive_poll_failures >= ASYNC_POLL_FAILURE_LIMIT:
                        raise_poll_abort(
                            f"异步任务连续 {ASYNC_POLL_FAILURE_LIMIT} 次轮询失败，已停止轮询；"
                            f"上游任务可能仍在计费，请稍后在中转站确认: {exc}",
                            exc,
                        )
                time.sleep(self.async_poll_interval)
                continue
            status_data = self._unwrap_response_data_object(status_data)
            status = str(status_data.get("status", "")).strip().lower()
            if status not in {"completed", "failed"}:
                try:
                    self._ensure_success_status(status_response, status_data, provider)
                except ProviderRequestError as exc:
                    if not handle_poll_error(exc):
                        consecutive_poll_failures += 1
                        if consecutive_poll_failures >= ASYNC_POLL_FAILURE_LIMIT:
                            raise_poll_abort(
                                f"异步任务连续 {ASYNC_POLL_FAILURE_LIMIT} 次轮询失败，已停止轮询；"
                                f"上游任务可能仍在计费，请稍后在中转站确认: {exc}",
                                exc,
                            )
                    time.sleep(self.async_poll_interval)
                    continue
            consecutive_poll_failures = 0
            if task_id:
                self.store.update(
                    task_id,
                    poll_count=poll_count,
                    last_poll_status=status or None,
                    last_poll_error=None,
                )
            if status == "completed":
                urls = self._extract_async_image_urls(status_data)
                if not urls:
                    exc = UpstreamTaskFailed(
                        "上游任务已完成但未返回图片 URL",
                        status_code=502,
                        details={"api_id": provider["id"], "api_name": provider["name"], "upstream": self._compact_error_payload(status_data)},
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
                    details={"api_id": provider["id"], "api_name": provider["name"], "upstream": self._compact_error_payload(status_data)},
                )
                exc.provider = provider
                exc.candidate = provider
                raise exc
            time.sleep(self.async_poll_interval)

    def _fnuu_async_submit_kwargs(
        self, provider: dict[str, Any], payload: dict[str, Any], operation: str
    ) -> dict[str, Any]:
        prompt = payload.get("edit_reference_prompt") if operation == "edit" else payload["prompt"]
        body: dict[str, Any] = {
            "model": provider.get("model") or DEFAULT_MODEL,
            "prompt": prompt,
            "n": payload["n"],
        }
        if payload["size"] != "auto":
            body["size"] = payload["size"]

        refs = self._edit_image_sequence(payload) if operation == "edit" else payload.get("reference_images") or []
        if refs:
            if len(refs) == 1 and self._is_local_reference(refs[0]):
                raw, mime, filename = self._reference_image_bytes(refs[0], "image")
                return {
                    "headers": self._auth_headers(provider),
                    "data": {key: str(value) for key, value in body.items()},
                    "files": {"image": (filename or f"image.{self._ext_for_mime(mime)}", raw, mime)},
                    "timeout": self._provider_timeout(provider),
                }
            images = [
                self._reference_for_json_image(ref, f"image[{index}]", max_bytes=FNUU_MAX_IMAGE_BYTES)
                for index, ref in enumerate(refs)
            ]
            body["image"] = images[0] if len(images) == 1 else images

        return {
            "headers": self._auth_headers(provider, json_content=True),
            "json": body,
            "timeout": self._provider_timeout(provider),
        }

    def _extract_async_image_urls(self, data: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        def add(candidate: Any) -> None:
            if not isinstance(candidate, str):
                return
            text = candidate.strip()
            if not text:
                return
            if text.startswith("data:image/") and ";base64," in text:
                value = text
            elif text.startswith(("http://", "https://")):
                value = text
            else:
                for found in self._extract_image_urls_from_text(text):
                    if found not in seen:
                        seen.add(found)
                        urls.append(found)
                return
            if value not in seen:
                seen.add(value)
                urls.append(value)

        def visit(value: Any) -> None:
            if isinstance(value, str):
                add(value)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if isinstance(value, dict):
                b64 = value.get("b64_json")
                if isinstance(b64, str) and b64.strip():
                    add(f"data:image/png;base64,{b64.strip()}")
                for key in ("url", "urls", "result", "data", "image", "images", "output"):
                    if key in value:
                        visit(value.get(key))

        for key in ("urls", "result", "data", "image", "images", "output"):
            if key in data:
                visit(data.get(key))
        return urls

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
            "reference_images": task.get("reference_images") or [],
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
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Accept": "application/json",
            "User-Agent": "gpt-img2-creater/0.1",
        }
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
            text = str(getattr(response, "text", "") or "")
            # Gateway-timeout pages must win over URL mining: their HTML can
            # embed unrelated asset URLs, and treating them as a completed
            # result would also skip the anti-double-billing timeout handling.
            is_gateway_timeout = self._is_gateway_timeout_response(
                getattr(response, "status_code", None),
                self._extract_html_title(text),
                text,
            )
            urls = [] if is_gateway_timeout else self._extract_image_urls_from_text(text)
            if urls:
                logger.info(
                    "[image] non-json response fallback api_id=%s api_name=%s status_code=%s urls=%d",
                    provider.get("id"),
                    provider.get("name"),
                    getattr(response, "status_code", None),
                    len(urls),
                )
                return {
                    "status": "completed",
                    "urls": urls,
                    "data": [{"url": url} for url in urls],
                    "non_json_url_fallback": True,
                    "raw_text": text[:1000],
                }
            details = self._non_json_response_details(response, provider, text, exc)
            logger.warning(
                "[image] non-json response api_id=%s api_name=%s status=%s content_type=%s server=%s cf_ray=%s html_title=%s",
                provider.get("id"),
                provider.get("name"),
                details.get("http_status"),
                details.get("content_type"),
                details.get("server"),
                details.get("cf_ray"),
                details.get("html_title"),
            )
            if details.get("gateway_timeout"):
                raise ProviderTimeoutError(
                    "API 节点网关超时，返回 HTML 而不是 OpenAI JSON",
                    status_code=504,
                    details=details,
                ) from exc
            raise ProviderRequestError(
                "API 节点返回的不是合法 JSON",
                status_code=502,
                details=details,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderRequestError(
                "API 节点 JSON 响应必须是对象",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )
        return data

    def _non_json_response_details(
        self,
        response: Any,
        provider: dict[str, Any],
        text: str,
        parse_error: Exception,
    ) -> dict[str, Any]:
        status_code = getattr(response, "status_code", None)
        content_type = self._response_header(response, "Content-Type")
        server = self._response_header(response, "Server")
        cf_ray = self._response_header(response, "CF-RAY")
        html_title = self._extract_html_title(text)
        is_html = self._is_html_response(text, content_type)
        cloudflare = bool(cf_ray or "cloudflare" in server.lower())
        gateway_timeout = self._is_gateway_timeout_response(status_code, html_title, text)
        details: dict[str, Any] = {
            "api_id": provider["id"],
            "api_name": provider["name"],
            "status_code": status_code,
            "http_status": status_code,
            "content_type": content_type,
            "parse_error": str(parse_error),
            "text_preview": text[:1000],
            "is_html_response": is_html,
        }
        if server:
            details["server"] = server
        if cf_ray:
            details["cf_ray"] = cf_ray
        if html_title:
            details["html_title"] = html_title
        if cloudflare:
            details["cloudflare"] = True
        if gateway_timeout:
            details["gateway_timeout"] = True
        if is_html:
            gateway = "Cloudflare/网关返回了 HTML 页面，响应没有按 OpenAI 兼容接口返回 JSON"
            if gateway_timeout:
                gateway += "；这是网关超时，不是后端 JSON 解析失败"
            if cloudflare:
                gateway += "；如果中转站没有调用日志，通常表示请求在 Cloudflare/网关层被拦截或超时"
            details["gateway_hint"] = gateway
        return details

    @staticmethod
    def _response_header(response: Any, name: str) -> str:
        headers = getattr(response, "headers", {}) or {}
        if not hasattr(headers, "get"):
            return ""
        return str(headers.get(name) or headers.get(name.lower()) or headers.get(name.upper()) or "").strip()

    @staticmethod
    def _extract_html_title(text: str) -> str:
        match = _HTML_TITLE_RE.search(str(text or ""))
        if not match:
            return ""
        return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip())

    @staticmethod
    def _is_html_response(text: str, content_type: str = "") -> bool:
        lowered_type = str(content_type or "").lower()
        if "html" in lowered_type:
            return True
        lowered = str(text or "").lstrip()[:200].lower()
        return lowered.startswith("<!doctype html") or lowered.startswith("<html") or "<html" in lowered

    @staticmethod
    def _is_gateway_timeout_response(status_code: Any, html_title: str = "", text: str = "") -> bool:
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = 0
        if code in {504, 522, 524}:
            return True
        timeout_text = f"{html_title} {str(text or '')[:500]}".lower()
        return "gateway timeout" in timeout_text or "timeout occurred" in timeout_text or "timed out" in timeout_text

    @staticmethod
    def _extract_image_urls_from_text(text: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for match in _IMAGE_URL_RE.finditer(str(text or "")):
            candidate = html.unescape(match.group(0).strip()).rstrip(".,;:]}")
            if not candidate:
                continue
            if candidate.lower().startswith("data:image/"):
                if ";base64," not in candidate:
                    continue
            else:
                parsed = urlparse(candidate)
                if parsed.scheme not in {"http", "https"}:
                    continue
                if not ImageService._looks_like_image_url(parsed):
                    continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
        return urls

    @staticmethod
    def _looks_like_image_url(parsed: Any) -> bool:
        path = str(parsed.path or "").lower()
        if path.endswith(_IMAGE_EXTENSIONS):
            return True
        query = unquote(str(parsed.query or "").lower())
        if "image/" in query:
            return True
        return any(marker in query for marker in ("format=png", "format=jpg", "format=jpeg", "format=webp"))

    def _ensure_success_status(self, response: Any, data: dict[str, Any], provider: dict[str, Any]) -> None:
        status_code = int(getattr(response, "status_code", 200) or 200)
        if data.get("non_json_url_fallback") and data.get("data"):
            return
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

    @classmethod
    def _compact_error_payload(cls, value: Any) -> Any:
        """Deep-copy an upstream payload for error details, truncating huge
        strings (e.g. b64_json) so /api/status and session.json stay small."""
        if isinstance(value, dict):
            return {key: cls._compact_error_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._compact_error_payload(item) for item in value]
        if isinstance(value, str) and len(value) > 2000:
            return f"{value[:200]}…(截断 {len(value) - 200} 字符)"
        return value

    @staticmethod
    def _is_poll_not_found_error(exc: Exception) -> bool:
        if not isinstance(exc, ImageServiceError):
            return False
        details = getattr(exc, "details", {}) or {}
        try:
            status_code = int(details.get("http_status") or details.get("status_code") or 0)
        except (TypeError, ValueError):
            status_code = 0
        return status_code == 404

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
            try:
                persisted.append(self._persist_one_result(provider, history_id, url, index))
            except Exception as exc:  # noqa: BLE001 - 生成已成功，落盘失败时保留原始 URL 而不是整体失败
                logger.warning(
                    "[image] result persist failed history_id=%s index=%d url=%s error=%s",
                    history_id,
                    index,
                    url if not str(url).startswith("data:") else "data:...",
                    exc,
                )
                persisted.append(url)
        return persisted

    def _persist_reference_images(self, history_id: str, refs: list[str]) -> list[str]:
        if not refs:
            return []
        if not self.persist_results:
            return refs
        target_dir_id = str(history_id)
        target_dir = self.result_dir / target_dir_id / "references"
        target_dir.mkdir(parents=True, exist_ok=True)
        persisted: list[str] = []
        for index, ref in enumerate(refs):
            text = str(ref or "").strip()
            if text.startswith(("http://", "https://")):
                persisted.append(text)
                continue
            if text.startswith("/api/results/") and self._result_url_belongs_to_history(text, target_dir_id):
                self._local_result_url_to_path(text, f"reference_images[{index}]")
                persisted.append(text)
                continue
            raw, mime, _ = self._reference_image_bytes(ref, f"reference_images[{index}]")
            ext = self._ext_for_mime(mime)
            filename = f"ref-{index}-{uuid.uuid4().hex[:8]}.{ext}"
            (target_dir / filename).write_bytes(raw)
            persisted.append(f"/api/results/{target_dir_id}/references/{filename}")
        return persisted

    @staticmethod
    def _all_urls_are_persisted_results(urls: list[str]) -> bool:
        return bool(urls) and all(str(url).startswith("/api/results/") for url in urls)

    @staticmethod
    def _result_url_belongs_to_history(value: str, history_id: str) -> bool:
        parsed = urlparse(str(value or ""))
        path = unquote(parsed.path or "")
        prefix = "/api/results/"
        if not path.startswith(prefix):
            return False
        relative = path[len(prefix) :].lstrip("/")
        first = relative.split("/", 1)[0]
        return first == str(history_id)

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
        reference_images: list[str],
        provider: dict[str, Any],
        task_id: str,
        attempts: list[dict[str, Any]],
        expires_at: Any,
        response_meta: dict[str, Any] | None = None,
    ) -> None:
        if not self.persist_results:
            return
        with self._manifest_lock(str(session_id)):
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
            if status != "completed" and previous.get("status") == "completed" and previous.get("urls"):
                # 新的一次提交失败/未完成时不清掉已成功的结果，只在附加字段里
                # 记录最近一次尝试，避免会话从 /api/sessions 列表里消失。
                manifest = dict(previous)
                manifest["last_task_id"] = task_id
                manifest["last_status"] = status
                manifest["last_error"] = (response_meta or {}).get("error") or None
                manifest["last_attempts"] = attempts
            else:
                manifest = {
                    "id": str(session_id),
                    "task_id": task_id,
                    "prompt": payload.get("prompt", ""),
                    "mode": operation,
                    "size": payload.get("size"),
                    "n": payload.get("n"),
                    "status": status,
                    "urls": urls,
                    "reference_images": reference_images,
                    "api_id": provider.get("id"),
                    "api_name": provider.get("name"),
                    "attempts": attempts,
                    "expires_at": expires_at,
                    "response_meta": response_meta or {},
                    "created_at": previous.get("created_at") or now,
                    "updated_at": now,
                }
            tmp_path = manifest_path.with_name(f"session.json.{uuid.uuid4().hex}.tmp")
            tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(manifest_path)

    def _manifest_lock(self, session_id: str) -> threading.Lock:
        with self._manifest_locks_guard:
            lock = self._manifest_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._manifest_locks[session_id] = lock
            return lock

    def _persist_failed_session_manifest(
        self,
        *,
        history_id: str | None,
        payload: dict[str, Any],
        operation: str,
        task_id: str,
        attempts: list[dict[str, Any]],
        error: str,
        status: str = "failed",
    ) -> None:
        if not history_id:
            return
        self._persist_session_manifest(
            session_id=history_id,
            payload=payload,
            operation=operation,
            status=status,
            urls=[],
            reference_images=list(payload.get("reference_images") or []),
            provider={},
            task_id=task_id,
            attempts=attempts,
            expires_at=None,
            response_meta={"error": error} if error else {},
        )

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
            "prompt": payload.get("edit_reference_prompt") if operation == "edit" else payload["prompt"],
            "n": payload["n"],
        }
        if payload["size"] != "auto":
            body["size"] = payload["size"]
        if payload.get("quality"):
            body["quality"] = payload["quality"]
        if payload.get("reference_images") and operation != "edit":
            body["reference_images"] = [
                self._reference_for_json_image(ref, f"reference_images[{index}]")
                for index, ref in enumerate(payload["reference_images"])
            ]

        if operation == "edit":
            self._add_edit_images_to_json_body(body, payload)

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
        refs = self._edit_image_sequence(payload) if operation == "edit" else payload.get("reference_images") or []
        prompt = payload.get("edit_reference_prompt") if operation == "edit" else payload["prompt"]
        if refs:
            content = [{"type": "text", "text": prompt}]
            for index, ref in enumerate(refs):
                # refs may be local /api/results/ URLs (persisted references) —
                # upstream cannot fetch those, so inline them as data URLs.
                image_url = self._reference_for_json_image(ref, f"images[{index}]")
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": prompt}]
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

        # 3) choices[].message — the actual Chat Completions shape. Providers
        # return image URLs/data URIs inside message.content (string or
        # content-part list) or a message.images array.
        choices = data.get("choices")
        if isinstance(choices, list):
            urls = []
            seen: set[str] = set()

            def add_url(candidate: Any) -> None:
                text = str(candidate or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    urls.append(text)

            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    for url in self._extract_image_urls_from_text(content):
                        add_url(url)
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "image_url":
                            image_url = part.get("image_url")
                            add_url(image_url.get("url") if isinstance(image_url, dict) else image_url)
                        elif isinstance(part.get("text"), str):
                            for url in self._extract_image_urls_from_text(part["text"]):
                                add_url(url)
                for image in message.get("images") or []:
                    if isinstance(image, dict):
                        image_url = image.get("image_url")
                        add_url(image_url.get("url") if isinstance(image_url, dict) else image.get("url"))
                    elif isinstance(image, str):
                        add_url(image)
            if urls:
                return urls

        # This is genuinely a non-image chat — that's a provider error for an
        # image tool.
        raise ProviderRequestError(
            "Chat Completions 响应中未找到图片数据 (data/output/choices)",
            status_code=502,
            details={"api_id": provider["id"], "api_name": provider["name"], "response": self._compact_error_payload(data)},
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

    def _normalize_reference_images(self, value: Any) -> list[str]:
        """Validate optional reference images while preserving their transport
        form: data URL, public URL, local result URL, or local file path."""
        if value is None:
            return []
        if not isinstance(value, list):
            raise GenerationValidationError("reference_images 必须是数组", status_code=400)
        if len(value) > 8:
            raise GenerationValidationError("参考图数量不能超过 8 张", status_code=400)
        refs: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            if text.startswith("data:image/") and ";base64," in text:
                refs.append(text)
                continue
            if self._is_public_image_url(text):
                refs.append(text)
                continue
            if text.startswith("/api/results/"):
                self._local_result_url_to_path(text, "参考图")
                refs.append(text)
                continue
            if self._looks_like_local_file_reference(text):
                self._local_file_reference_path(text, "参考图")
                refs.append(text)
                continue
            raise GenerationValidationError(
                "参考图必须是 data:image/*;base64、本地 /api/results/ 链接、公网图片 URL 或本地图片路径",
                status_code=400,
            )
        return refs

    @staticmethod
    def _is_public_image_url(value: str) -> bool:
        parsed = urlparse(str(value or ""))
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _looks_like_local_file_reference(value: str) -> bool:
        text = str(value or "").strip()
        if not text or text.startswith(("http://", "https://", "data:")):
            return False
        return Path(text).expanduser().is_file()

    def _is_local_reference(self, value: str) -> bool:
        text = str(value or "").strip()
        return text.startswith("/api/results/") or self._looks_like_local_file_reference(text)

    def _reference_for_json_image(self, value: str, field_name: str, max_bytes: int | None = None) -> str:
        text = str(value or "").strip()
        if text.startswith("data:image/") and ";base64," in text:
            raw, _ = self._decode_data_url(text, field_name)
            self._ensure_reference_size(raw, field_name, max_bytes)
            return text
        if self._is_public_image_url(text):
            return text
        raw, mime, _ = self._reference_image_bytes(text, field_name, max_bytes=max_bytes)
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _reference_image_bytes(
        self,
        value: str,
        field_name: str,
        max_bytes: int | None = None,
    ) -> tuple[bytes, str, str]:
        text = str(value or "").strip()
        if text.startswith("data:image/") and ";base64," in text:
            raw, mime = self._decode_data_url(text, field_name)
            self._ensure_reference_size(raw, field_name, max_bytes)
            return raw, mime, f"{field_name}.{self._ext_for_mime(mime)}"
        if text.startswith("/api/results/"):
            path = self._local_result_url_to_path(text, field_name)
            return self._local_image_path_bytes(path, field_name, max_bytes=max_bytes)
        if self._is_public_image_url(text):
            raise GenerationValidationError(f"{field_name} 是公网 URL，不能作为 multipart 文件上传", status_code=400)
        path = self._local_file_reference_path(text, field_name)
        return self._local_image_path_bytes(path, field_name, max_bytes=max_bytes)

    def _local_result_url_to_data_url(self, value: str, field_name: str) -> str:
        raw, mime, _ = self._reference_image_bytes(value, field_name)
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _local_result_url_to_path(self, value: str, field_name: str) -> Path:
        parsed = urlparse(str(value or ""))
        path = unquote(parsed.path or "")
        prefix = "/api/results/"
        if not path.startswith(prefix):
            raise GenerationValidationError(f"{field_name} 只能引用本地 /api/results/ 图片", status_code=400)
        relative = path[len(prefix) :].lstrip("/")
        if not relative:
            raise GenerationValidationError(f"{field_name} 本地图片路径为空", status_code=400)
        root = self.result_dir.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise GenerationValidationError(f"{field_name} 本地图片路径非法", status_code=400) from exc
        if not candidate.is_file():
            raise GenerationValidationError(f"{field_name} 本地图片不存在", status_code=400)
        return candidate

    def _local_file_reference_path(self, value: str, field_name: str) -> Path:
        candidate = Path(str(value or "").strip()).expanduser().resolve()
        if not candidate.is_file():
            raise GenerationValidationError(f"{field_name} 本地图片不存在", status_code=400)
        return candidate

    def _local_image_path_bytes(
        self,
        path: Path,
        field_name: str,
        max_bytes: int | None = None,
    ) -> tuple[bytes, str, str]:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        if not mime.startswith("image/"):
            raise GenerationValidationError(f"{field_name} 本地文件不是图片", status_code=400)
        raw = path.read_bytes()
        self._ensure_reference_size(raw, field_name, max_bytes)
        return raw, mime, path.name

    @staticmethod
    def _ensure_reference_size(raw: bytes, field_name: str, max_bytes: int | None = None) -> None:
        if max_bytes is not None and len(raw) > max_bytes:
            raise GenerationValidationError(f"{field_name} 图片大小不能超过 12MB", status_code=400)

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
