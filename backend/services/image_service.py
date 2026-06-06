from __future__ import annotations

from typing import Any

from .config_service import ConfigNotFoundError, ConfigService, DEFAULT_MODEL


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


class AllProvidersFailed(ImageServiceError):
    pass


class ImageService:
    def __init__(
        self,
        config_service: ConfigService | None = None,
        http_client: Any | None = None,
        request_timeout: int = 30,
    ):
        self.config_service = config_service or ConfigService()
        if http_client is None:
            import requests

            http_client = requests.Session()
        self.http_client = http_client
        self.request_timeout = request_timeout

    def submit_generation(self, prompt: str, size: str = "1024x1024", n: int = 1) -> dict[str, Any]:
        prompt = self._normalize_prompt(prompt)
        n = self._normalize_image_count(n)
        size = self._normalize_size(size)
        payload = {
            "model": DEFAULT_MODEL,
            "prompt": prompt,
            "size": size,
            "n": n,
        }
        return self._submit_with_fallback(payload=payload, operation="generate")

    def submit_edit_generation(
        self,
        prompt: str,
        image: str,
        mask: str,
        size: str = "1024x1024",
        n: int = 1,
        edit_mode: str = "mask",
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = self._normalize_prompt(prompt)
        n = self._normalize_image_count(n)
        size = self._normalize_size(size)
        image = self._normalize_image_data_url(image, "image")
        mask = self._normalize_image_data_url(mask, "mask")
        edit_mode = str(edit_mode or "mask").strip() or "mask"
        if edit_mode not in {"mask", "selection"}:
            raise GenerationValidationError("edit_mode 只支持 mask 或 selection", status_code=400)
        if selection is not None and not isinstance(selection, dict):
            raise GenerationValidationError("selection 必须是对象", status_code=400)

        payload = {
            "model": DEFAULT_MODEL,
            "prompt": prompt,
            "size": size,
            "n": n,
            "image": image,
            "mask": mask,
            "edit_mode": edit_mode,
        }
        if selection is not None:
            payload["selection"] = selection
        return self._submit_with_fallback(payload=payload, operation="edit")

    def _submit_with_fallback(self, payload: dict[str, Any], operation: str) -> dict[str, Any]:
        providers = self.config_service.get_enabled_configs()
        attempts: list[dict[str, Any]] = []

        if not providers:
            raise AllProvidersFailed("没有可用的 API 配置，请先启用至少一个节点", status_code=400)

        # Fallback 核心逻辑：
        # 1. 配置文件中的数组顺序就是优先级，前端拖拽排序后会持久化这个顺序。
        # 2. 每个节点只负责“提交异步任务”，不在这里等待生成完成，避免 Flask 请求阻塞 1-3 分钟。
        # 3. 任一节点出现认证失败、超时、5xx、返回结构缺失 task_id 等问题，都记录失败并切到下一个启用节点。
        for provider in providers:
            try:
                result = self._submit_with_provider(provider, payload=payload, operation=operation)
            except Exception as exc:
                attempts.append(self._failed_attempt(provider, exc))
                continue

            attempts.append({"api_id": provider["id"], "api_name": provider["name"], "ok": True})
            result["attempts"] = attempts
            return result

        raise AllProvidersFailed(
            "所有 API 节点提交任务均失败",
            status_code=502,
            details={"attempts": attempts},
        )

    def poll_generation_status(self, api_id: str, task_id: str) -> dict[str, Any]:
        if not str(api_id or "").strip():
            raise GenerationValidationError("api_id 不能为空", status_code=400)
        if not str(task_id or "").strip():
            raise GenerationValidationError("task_id 不能为空", status_code=400)

        try:
            provider = self.config_service.get_config(api_id)
        except ConfigNotFoundError as exc:
            raise GenerationValidationError(f"找不到任务对应的 API 配置: {api_id}", status_code=404) from exc

        url = f"{provider['base_url'].rstrip('/')}/async/images/{task_id}"
        try:
            response = self.http_client.get(
                url,
                headers=self._headers(provider),
                timeout=self.request_timeout,
            )
            data = self._parse_response_json(response, provider)
            self._ensure_success_status(response, data, provider)
        except ImageServiceError:
            raise
        except Exception as exc:
            raise ProviderRequestError(
                "查询任务状态失败",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "error": str(exc)},
            ) from exc

        status = str(data.get("status", "")).strip().lower()
        if not status:
            raise ProviderRequestError(
                "状态查询返回缺少 status 字段",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )

        # 轮询只代理一次中转站状态查询；4 秒递归/定时轮询由前端负责，后端保持无状态。
        return {
            "api_id": provider["id"],
            "api_name": provider["name"],
            "task_id": task_id,
            "status": status,
            "urls": data.get("urls") if isinstance(data.get("urls"), list) else [],
            "expires_at": data.get("expires_at"),
            "error": data.get("error"),
            "raw": data,
        }

    def _submit_with_provider(self, provider: dict[str, Any], payload: dict[str, Any], operation: str) -> dict[str, Any]:
        url = f"{provider['base_url'].rstrip('/')}/async/images"
        request_payload = dict(payload)
        request_payload["model"] = provider.get("model") or payload.get("model") or DEFAULT_MODEL
        response = self.http_client.post(
            url,
            headers=self._headers(provider, json_content=True),
            json=request_payload,
            timeout=self.request_timeout,
        )
        data = self._parse_response_json(response, provider)
        self._ensure_success_status(response, data, provider)

        task_id = data.get("task_id")
        if not task_id:
            raise ProviderRequestError(
                "提交任务成功响应缺少 task_id",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )

        return {
            "task_id": task_id,
            "api_id": provider["id"],
            "api_name": provider["name"],
            "status": data.get("status", "queued"),
            "poll_url": data.get("poll_url"),
            "model": request_payload["model"],
            "operation": operation,
            "raw": data,
        }

    def _headers(self, provider: dict[str, Any], json_content: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {provider['api_key']}"}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def _parse_response_json(self, response: Any, provider: dict[str, Any]) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise ProviderRequestError(
                "中转站返回的不是合法 JSON",
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
                "中转站 JSON 响应必须是对象",
                status_code=502,
                details={"api_id": provider["id"], "api_name": provider["name"], "response": data},
            )
        return data

    def _ensure_success_status(self, response: Any, data: dict[str, Any], provider: dict[str, Any]) -> None:
        status_code = int(getattr(response, "status_code", 200) or 200)
        if 200 <= status_code < 300:
            return
        message = data.get("error") or data.get("message") or getattr(response, "text", "") or "请求失败"
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

    def _failed_attempt(self, provider: dict[str, Any], exc: Exception) -> dict[str, Any]:
        details = getattr(exc, "details", {}) if isinstance(exc, ImageServiceError) else {}
        return {
            "api_id": provider["id"],
            "api_name": provider["name"],
            "ok": False,
            "error": str(exc),
            "details": details,
        }

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
        value = str(size or "").strip()
        parts = value.split("x")
        if len(parts) != 2 or not all(part.isdigit() and int(part) > 0 for part in parts):
            raise GenerationValidationError("size 必须形如 1024x1024", status_code=400)
        return value

    @staticmethod
    def _normalize_image_data_url(value: str, field_name: str) -> str:
        text = str(value or "").strip()
        if not text.startswith("data:image/") or ";base64," not in text:
            raise GenerationValidationError(f"{field_name} 必须是 data:image/*;base64 格式", status_code=400)
        return text
