from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "data" / "prompt_templates.json"

DEFAULT_TEMPLATES = [
    {
        "id": "seed-photo-1",
        "title": "写实摄影：雨夜街头",
        "text": "一位年轻女子在雨夜的东京街头，霓虹灯倒映在潮湿的路面上，35mm 胶片拍摄风格",
    },
    {
        "id": "seed-photo-2",
        "title": "写实摄影：日落海岸",
        "text": "壮丽的日落海岸线，长曝光波浪模糊成雾，电影级暖色调",
    },
    {
        "id": "seed-anime-1",
        "title": "动漫插画：飞行城堡",
        "text": "吉卜力工作室风格，一座漂浮在云海之上的飞行城堡，金色夕阳光照，细节丰富",
    },
    {
        "id": "seed-product-1",
        "title": "产品摄影：耳机",
        "text": "极简产品摄影，一副无线耳机放在大理石台面上，柔和工作室打光，干净背景",
    },
    {
        "id": "seed-watercolor-1",
        "title": "水彩艺术：花园",
        "text": "盛开的花园，印象派水彩风格，明亮色彩，湿润笔触",
    },
    {
        "id": "seed-cyberpunk-1",
        "title": "赛博朋克：城市",
        "text": "赛博朋克城市景观，高耸的摩天大楼布满全息广告，飞行载具穿梭，紫青色霓虹色调",
    },
    {
        "id": "seed-edit-1",
        "title": "局部编辑：替换背景",
        "text": "只修改被标记区域：将背景替换为星空夜景，保持主体不变，边缘自然融合",
    },
    {
        "id": "seed-edit-2",
        "title": "局部编辑：改变颜色",
        "text": "只修改被标记区域：将汽车颜色改为金属红，保持光照、材质和反射自然",
    },
]


class PromptTemplateServiceError(Exception):
    """Base error for prompt-template JSON persistence failures."""


class PromptTemplateValidationError(PromptTemplateServiceError, ValueError):
    """Raised when a template payload is invalid."""


class PromptTemplateNotFoundError(PromptTemplateServiceError, KeyError):
    """Raised when a requested template id does not exist."""


class PromptTemplateService:
    def __init__(self, template_path: str | Path | None = None):
        self.template_path = Path(template_path) if template_path else DEFAULT_TEMPLATE_PATH
        self._lock = threading.RLock()
        self._ensure_storage()

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read_store()["templates"]]

    def create_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            now = self._now()
            template = self._normalize_template(payload)
            template["id"] = str(payload.get("id") or uuid.uuid4().hex)
            template["created_at"] = now
            template["updated_at"] = now
            store["templates"].insert(0, template)
            self._write_store(store)
            return dict(template)

    def update_template(self, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            for index, current in enumerate(store["templates"]):
                if current["id"] == template_id:
                    merged = dict(current)
                    merged.update(payload or {})
                    updated = self._normalize_template(merged)
                    updated["id"] = current["id"]
                    updated["created_at"] = current.get("created_at", self._now())
                    updated["updated_at"] = self._now()
                    store["templates"][index] = updated
                    self._write_store(store)
                    return dict(updated)
        raise PromptTemplateNotFoundError(f"提示词模板不存在: {template_id}")

    def delete_template(self, template_id: str) -> None:
        with self._lock:
            store = self._read_store()
            next_templates = [item for item in store["templates"] if item["id"] != template_id]
            if len(next_templates) == len(store["templates"]):
                raise PromptTemplateNotFoundError(f"提示词模板不存在: {template_id}")
            store["templates"] = next_templates
            self._write_store(store)

    def _ensure_storage(self) -> None:
        with self._lock:
            self.template_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.template_path.exists():
                now = self._now()
                self._write_store(
                    {
                        "templates": [
                            {**template, "created_at": now, "updated_at": now}
                            for template in DEFAULT_TEMPLATES
                        ]
                    }
                )

    def _read_store(self) -> dict[str, list[dict[str, Any]]]:
        try:
            raw = self.template_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {"templates": []}
        except json.JSONDecodeError as exc:
            raise PromptTemplateServiceError(f"提示词模板 JSON 格式错误: {self.template_path}") from exc
        except OSError as exc:
            raise PromptTemplateServiceError(f"读取提示词模板失败: {self.template_path}") from exc

        if isinstance(data, list):
            data = {"templates": data}
        if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
            raise PromptTemplateServiceError("提示词模板文件结构必须为 {'templates': [...]}")
        return {"templates": [dict(item) for item in data["templates"] if isinstance(item, dict)]}

    def _write_store(self, store: dict[str, Any]) -> None:
        payload = json.dumps(store, ensure_ascii=False, indent=2)
        tmp_path = self.template_path.with_suffix(f"{self.template_path.suffix}.tmp")
        try:
            self.template_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(payload + "\n", encoding="utf-8")
            tmp_path.replace(self.template_path)
        except OSError as exc:
            raise PromptTemplateServiceError(f"写入提示词模板失败: {self.template_path}") from exc

    @staticmethod
    def _normalize_template(payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip() or "未命名"
        text = str(payload.get("text") or "").strip()
        if not text:
            raise PromptTemplateValidationError("模板内容不能为空")
        return {"id": payload.get("id"), "title": title, "text": text}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
